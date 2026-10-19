import time

import pytest

from app.api.live import COOKIE
from app.storage.database import Database
from app.web_auth import WebAuth
from tests.test_calls import service

ORIGIN = {'Origin': 'https://example.test'}
PASSWORD = 'test-only long password 🔑'
NEXT_PASSWORD = 'a different test password'


def paired(client):
    client.base_url = 'https://example.test'
    auth = client.app.state.web_auth
    token = auth.login(auth.create_code())
    client.cookies.set(COOKIE, token)
    return auth, token


def save(client, **changes):
    auth = client.app.state.web_auth
    csrf = auth.session(client.cookies.get(COOKIE))['csrf']
    data = {'csrf': csrf, 'new_password': PASSWORD, 'confirm_password': PASSWORD, **changes}
    # Clear the directly seeded cookie to avoid duplicate cookie-domain fixtures.
    token = client.cookies.get(COOKIE)
    client.cookies.clear()
    client.cookies.set(COOKIE, token, domain='example.test', path='/')
    return client.post('/live/password', data=data, headers=ORIGIN, follow_redirects=False)


def test_first_password_and_manager_compatible_forms(service):
    client, _, _ = service
    auth, token = paired(client)
    assert not auth.password_enabled()
    page = client.get('/live/password')
    assert 'autocomplete="username"' in page.text
    assert 'autocomplete="new-password"' in page.text
    assert 'current_password' not in page.text
    assert save(client).status_code == 303
    assert auth.password_enabled() and auth.session(token) is None
    page = client.get('/live/password?saved=true')
    assert 'Password saved' in page.text and 'current_password' in page.text
    assert PASSWORD not in page.text
    login = client.get('/live/login')
    assert 'action="/live/password/login"' in login.text
    assert 'autocomplete="current-password"' in login.text
    assert 'id="pairing-login"' in login.text
    js = client.get('/live/login.js').text
    assert "getElementById('pairing-login')" in js
    assert "document.querySelector('form')" not in js


def test_password_login_persists_and_does_not_need_a_pairing_code(service):
    client, manager, _ = service
    auth, token = paired(client)
    auth.set_password(token, '', PASSWORD)
    restarted = WebAuth(Database(manager.db.path))
    assert restarted.password_enabled()
    token = restarted.password_login('owner', PASSWORD)
    assert token and auth.session(token)
    with manager.db.connect() as db:
        row = db.execute('SELECT * FROM web_password').fetchone()
    assert isinstance(row['salt'], bytes) and len(row['salt']) == 16
    assert isinstance(row['hash'], bytes) and len(row['hash']) == 64
    assert PASSWORD.encode() not in row['hash']
    client.cookies.clear()
    response = client.post('/live/password/login', data={'username': 'owner', 'password': PASSWORD},
                           headers=ORIGIN, follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == '/live'
    assert all(flag in response.headers['set-cookie'] for flag in ('Secure', 'HttpOnly', 'SameSite=strict'))
    assert client.get('/live/bootstrap').status_code == 200


@pytest.mark.parametrize('username,password', [('someone', PASSWORD), ('owner', 'wrong password'),
    ('owner', 'x' * 257), ('owner', ''), ('owner', 'tiny')])
def test_invalid_password_is_generic_and_never_reflected(service, username, password):
    client, _, _ = service
    auth, token = paired(client)
    auth.set_password(token, '', PASSWORD)
    client.cookies.clear()
    response = client.post('/live/password/login', data={'username': username, 'password': password}, headers=ORIGIN)
    assert response.status_code == 400 and 'Sign-in unsuccessful' in response.text
    assert COOKIE not in response.headers.get('set-cookie', '')
    assert PASSWORD not in response.text
    assert client.get('/live/bootstrap').status_code == 401


def test_password_setup_requires_owner_origin_csrf_and_confirmation(service):
    client, _, _ = service
    client.base_url = 'https://example.test'
    assert client.get('/live/password', follow_redirects=False).status_code == 303
    assert client.post('/live/password', data={}).status_code == 401
    auth, token = paired(client)
    assert client.post('/live/password', data={}).status_code == 403
    assert client.post('/live/password', data={'csrf': 'wrong'}, headers=ORIGIN).status_code == 403
    assert client.post('/live/password', data={'csrf': 'неправильно'}, headers=ORIGIN).status_code == 403
    assert save(client, confirm_password='different').status_code == 400
    assert not auth.password_enabled() and auth.session(token)
    assert save(client, new_password='short', confirm_password='short').status_code == 400
    assert not auth.password_enabled()
    assert client.post('/live/password/login', data={'username': 'owner', 'password': PASSWORD}).status_code == 403


def test_change_requires_current_password_and_revokes_other_sessions_and_codes(service):
    client, _, _ = service
    auth, token = paired(client)
    token = auth.set_password(token, '', PASSWORD)
    other = auth.password_login('owner', PASSWORD)
    unused = auth.create_code()
    with pytest.raises(ValueError, match='Current password'):
        auth.set_password(token, 'incorrect password', NEXT_PASSWORD)
    assert auth.session(token) and auth.session(other)
    replacement = auth.set_password(token, PASSWORD, NEXT_PASSWORD)
    assert not auth.session(token) and not auth.session(other)
    assert auth.session(replacement)
    assert auth.login(unused) is None
    assert auth.password_login('owner', PASSWORD) is None
    assert auth.password_login('owner', NEXT_PASSWORD)
    # Pairing remains available as a bootstrap/recovery route.
    assert auth.login(auth.create_code())


def test_password_attempt_budget_survives_restart_and_expires(service, monkeypatch):
    client, manager, _ = service
    auth, token = paired(client)
    auth.set_password(token, '', PASSWORD)
    stamp = time.time()
    monkeypatch.setattr('app.web_auth.time.time', lambda: stamp)
    for _ in range(10):
        assert auth.password_login('owner', 'wrong long password') is None
    restarted = WebAuth(Database(manager.db.path))
    assert restarted.password_login('owner', PASSWORD) is None
    monkeypatch.setattr('app.web_auth.time.time', lambda: stamp + 61)
    assert restarted.password_login('owner', PASSWORD)


def test_recovery_revokes_password_sessions_and_codes(service):
    client, _, _ = service
    auth, token = paired(client)
    token = auth.set_password(token, '', PASSWORD)
    code = auth.create_code()
    auth.reset_password()
    assert not auth.password_enabled() and not auth.session(token)
    assert auth.password_login('owner', PASSWORD) is None
    assert auth.login(code) is None
    token = auth.login(auth.create_code())
    assert auth.set_password(token, '', NEXT_PASSWORD)


def test_expired_session_cannot_change_password(service):
    client, manager, _ = service
    auth, token = paired(client)
    with manager.db.connect() as db:
        db.execute('UPDATE web_sessions SET expires=0')
    with pytest.raises(ValueError, match='Sign in again'):
        auth.set_password(token, '', PASSWORD)
    assert not auth.password_enabled()


def test_reset_during_password_verification_cannot_issue_a_session(service, monkeypatch):
    client, _, _ = service
    auth, token = paired(client)
    auth.set_password(token, '', PASSWORD)
    from app.web_auth import password_hash
    def reset_while_hashing(password, salt):
        result = password_hash(password, salt)
        auth.reset_password()
        return result
    monkeypatch.setattr('app.web_auth.password_hash', reset_while_hashing)
    assert auth.password_login('owner', PASSWORD) is None
    with auth.db.connect() as db:
        assert db.execute('SELECT count(*) FROM web_sessions').fetchone()[0] == 0


def test_revoked_session_during_password_hashing_cannot_change_credentials(service, monkeypatch):
    client, _, _ = service
    auth, token = paired(client)
    from app.web_auth import password_hash
    def revoke_while_hashing(password, salt):
        result = password_hash(password, salt)
        auth.logout(token)
        return result
    monkeypatch.setattr('app.web_auth.password_hash', revoke_while_hashing)
    with pytest.raises(ValueError, match='Sign in again'):
        auth.set_password(token, '', PASSWORD)
    assert not auth.password_enabled()
