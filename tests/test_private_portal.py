"""Private-overlay portal sessions remain separate from public HTTPS sessions."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.api.live import COOKIE, stream_events
from app.api.portal_access import PRIVATE_COOKIE
from app.config import Settings
from app.storage.models import CallRequest
from tests.test_calls import service

PRIVATE = 'http://192.168.20.10:8766'
PEER = '172.20.0.2'
PASSWORD = 'fixture-only portal password'


@pytest.fixture
def portal(service):
    public, manager, gateway = service
    manager.settings.private_portal_origin = PRIVATE
    manager.settings.private_portal_trusted_peers = [PEER]
    auth = public.app.state.web_auth
    token = auth.login(auth.create_code())
    auth.set_password(token, '', PASSWORD)
    private = TestClient(public.app, base_url=PRIVATE, client=(PEER, 12000))
    public.base_url = 'https://example.test'
    try:
        yield public, private, manager, auth
    finally:
        private.close()


def sign_in(client, origin=PRIVATE):
    return client.post('/live/password/login', headers={'Origin': origin},
        data={'username': 'owner', 'password': PASSWORD}, follow_redirects=False)


def test_private_password_login_works_over_http_without_changing_public_cookie(portal):
    public, private, manager, _ = portal
    page = private.get('/live/login')
    assert page.status_code == 200 and 'Private connection' in page.text
    assert 'autocomplete="current-password"' in page.text
    result = sign_in(private)
    assert result.status_code == 303 and result.headers['location'] == '/live'
    cookie = result.headers['set-cookie']
    assert PRIVATE_COOKIE in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie
    assert 'Secure' not in cookie and 'Domain=' not in cookie
    assert 'Path=/live' in cookie
    assert private.get('/live').status_code == 200
    assert private.get('/live/bootstrap').status_code == 200
    assert 'create_code' not in page.text
    result = sign_in(public, 'https://example.test')
    assert COOKIE in result.headers['set-cookie'] and 'Secure' in result.headers['set-cookie']
    assert public.get('/live/bootstrap').status_code == 200


@pytest.mark.parametrize('peer', ['198.51.100.9', '172.20.0.4', '192.168.20.99'])
def test_private_login_rejects_untrusted_peer_even_with_spoofed_forwarded_headers(portal, peer):
    public, _, _, _ = portal
    c = TestClient(public.app, base_url=PRIVATE, client=(peer, 12000))
    with c:
        headers = {'Origin': PRIVATE, 'X-Forwarded-For': PEER, 'X-Forwarded-Host': '192.168.20.10:8766',
                   'X-Forwarded-Proto': 'https'}
        assert c.get('/live/login', headers=headers).status_code == 403
        assert c.post('/live/password/login', headers=headers,
            data={'username': 'owner', 'password': PASSWORD}).status_code == 403


@pytest.mark.parametrize('origin', [None, 'null', 'https://evil.test', 'https://example.test', 'http://192.168.20.10:8767'])
def test_private_login_still_requires_its_exact_origin(portal, origin):
    _, private, _, _ = portal
    headers = {'Origin': origin} if origin is not None else {}
    result = private.post('/live/password/login', headers=headers, data={'username': 'owner', 'password': PASSWORD})
    assert result.status_code == 403 and 'set-cookie' not in result.headers


def test_origin_and_cookie_binding_prevent_cross_surface_replay(portal):
    public, private, _, auth = portal
    sign_in(private)
    private_token = private.cookies.get(PRIVATE_COOKIE)
    public.cookies.set(COOKIE, private_token)
    assert public.get('/live/bootstrap').status_code == 401
    public.cookies.clear()
    sign_in(public, 'https://example.test')
    public_token = public.cookies.get(COOKIE)
    private.cookies.clear();private.cookies.set(PRIVATE_COOKIE, public_token)
    assert private.get('/live/bootstrap').status_code == 401
    legacy_token = auth.login(auth.create_code())
    private.cookies.clear();private.cookies.set(PRIVATE_COOKIE, legacy_token)
    assert private.get('/live/bootstrap').status_code == 401
    public.cookies.clear();public.cookies.set(COOKIE, legacy_token)
    assert public.get('/live/bootstrap').status_code == 200
    with pytest.raises(ValueError, match='cannot move'):
        auth.bind_session_origin(public_token, PRIVATE)


def test_private_writes_keep_csrf_and_public_origin_checks(portal):
    public, private, _, auth = portal
    sign_in(private)
    csrf = auth.session(private.cookies.get(PRIVATE_COOKIE))['csrf']
    assert private.put('/live/voice', json={'voice': 'meridian'}, headers={'Origin': PRIVATE}).status_code == 403
    assert private.put('/live/voice', json={'voice': 'meridian'},
        headers={'Origin': 'https://example.test', 'X-CSRF-Token': csrf}).status_code == 403
    assert private.put('/live/voice', json={'voice': 'meridian'},
        headers={'Origin': PRIVATE, 'X-CSRF-Token': csrf}).status_code == 200
    sign_in(public, 'https://example.test')
    assert public.post('/live/password/login', headers={'Origin': PRIVATE},
        data={'username': 'owner', 'password': PASSWORD}).status_code == 403


def test_private_pairing_and_logout_do_not_revoke_public_session(portal):
    public, private, _, auth = portal
    sign_in(public, 'https://example.test');public_token = public.cookies.get(COOKIE)
    result = private.post('/live/login', headers={'Origin': PRIVATE, 'Accept': 'application/json'},
                          data={'code': auth.create_code()})
    assert result.status_code == 200
    token = private.cookies.get(PRIVATE_COOKIE)
    session = auth.session(token)
    assert private.post('/live/logout', headers={'Origin': PRIVATE, 'X-CSRF-Token': session['csrf']}).status_code == 204
    assert private.get('/live/bootstrap').status_code == 401
    assert auth.session(public_token) and not auth.session(token)
    with auth.db.connect() as db:
        assert not db.execute('SELECT 1 FROM web_session_origins WHERE origin=?', (PRIVATE,)).fetchone()


def test_private_sse_checks_the_private_session_and_stops_after_revocation(portal):
    public, private, manager, auth = portal
    sign_in(private);token = private.cookies.get(PRIVATE_COOKIE)
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    request = Request({'type': 'http', 'app': public.app, 'method': 'GET', 'scheme': 'http', 'path': '/live/events',
        'query_string': b'', 'headers': [(b'host', b'192.168.20.10:8766'), (b'cookie', (PRIVATE_COOKIE+'='+token).encode())],
        'client': (PEER, 12000), 'server': ('192.168.20.10', 8766)}, receive=receive)
    async def connected():
        cid = manager.db.create(CallRequest(phone_number='+12025550101', objective='Fictional stream check'))
        head = manager.db.event_head()
        manager.db.emit(cid, 'call.status', {'status': 'queued'})
        stream = stream_events(manager, head, time.time()+60, request)
        assert 'connected' in await anext(stream)
        assert 'call.status' in await anext(stream)
        auth.logout(token)
        assert 'expired' in await anext(stream)
        await stream.aclose()
    asyncio.run(connected())


def test_private_password_change_rebinds_the_replacement_session(portal):
    public, private, _, auth = portal
    sign_in(public, 'https://example.test');public_token = public.cookies.get(COOKIE)
    sign_in(private);old_token = private.cookies.get(PRIVATE_COOKIE)
    csrf = auth.session(old_token)['csrf']
    response = private.post('/live/password', headers={'Origin': PRIVATE}, data={
        'csrf': csrf, 'current_password': PASSWORD, 'new_password': 'replacement fixture password',
        'confirm_password': 'replacement fixture password'}, follow_redirects=False)
    assert response.status_code == 303
    assert PRIVATE_COOKIE in response.headers['set-cookie']
    assert not auth.session(old_token) and not auth.session(public_token)
    assert private.get('/live/password?saved=true').status_code == 200
    assert private.get('/live/bootstrap').status_code == 200


def test_action_url_bookmark_recovers_to_sign_in(portal):
    _, private, _, _ = portal
    response = private.get('/live/password/login', follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/live/login'


@pytest.mark.parametrize('origin,peers', [
    ('https://example.test', [PEER]), ('http://192.168.20.10/private', [PEER]),
    ('http://user@192.168.20.10', [PEER]), ('http://192.168.20.10:bad', [PEER]),
    (PRIVATE, []), ('', [PEER]), (PRIVATE, ['172.20.0.0/16']), (PRIVATE, ['8.8.8.8']),
    (PRIVATE, ['*'])])
def test_private_portal_configuration_requires_exact_private_origin_and_peers(origin, peers):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, private_portal_origin=origin, private_portal_trusted_peers=peers)


def test_private_mode_is_disabled_by_default(service):
    public, _, _ = service
    public.base_url = PRIVATE
    assert public.post('/live/password/login', headers={'Origin': PRIVATE}, data={}).status_code == 403
