import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from app.calling.call_manager import CallManager
from app.calling.twilio_client import stream_twiml
from app.config import Settings
from app.main import create_app
from app.mcp.server import create_mcp
from app.oauth_provider import PairingOAuthProvider
from app.storage.database import Database
from app.storage.models import CallRequest, DigitsRequest
from tests.test_calls import MockTwilio, service, create


@pytest.mark.parametrize('digits', ['', 'abc', '<Play>', '2' * 21, '１２', '1 2'])
def test_invalid_digits(digits):
    with pytest.raises(ValidationError):
        DigitsRequest(digits=digits, reason='Navigate menu')


def test_twilio_tones_before_reconnection():
    xml = stream_twiml(Settings(_env_file=None, public_base_url='https://example.test'), 'test', 'w2#')
    assert '<Play digits="w2#"' in xml
    assert xml.index('<Play') < xml.index('<Connect>') < xml.index('<Hangup')


def test_connected_keypad_reconnect_and_persistence(service):
    client, manager, gateway = service
    call_id = create(client).json()['call_id']
    manager.transition(call_id, 'in_progress')
    async def exercise():
        old = SimpleNamespace(handoff=False, closing=False, playback_sealed=False, end_requested=asyncio.Event())
        manager.attach_bridge(call_id, old)
        async def digits(sid, cid, digits):
            assert old.handoff and old.playback_sealed
            assert digits == '2#'
            new = SimpleNamespace(handoff=False)
            manager.attach_bridge(cid, new)
            manager.detach_bridge(cid, old)
            assert manager.bridges[cid] is new
        gateway.press_digits = digits
        result = await manager.press_digits(call_id, DigitsRequest(digits='2#', reason='Service department'))
        assert result['keypad_status'] == 'submitted_and_reconnected'
        assert gateway.status == 'queued'  # No hangup during handoff.
    asyncio.run(exercise())
    assert Database(manager.db.path).get(call_id)['keypad_events'][0]['digits'] == '2#'


def test_keypad_failure_ends_call_and_is_not_retried(service):
    client, manager, gateway = service
    call_id = create(client).json()['call_id']
    manager.transition(call_id, 'in_progress')
    gateway.status = 'in-progress'
    async def exercise():
        bridge = SimpleNamespace(handoff=False, closing=False, playback_sealed=False, end_requested=asyncio.Event())
        manager.attach_bridge(call_id, bridge)
        async def failed(*args):
            raise RuntimeError('provider-secret')
        gateway.press_digits = failed
        with pytest.raises(RuntimeError, match='unconfirmed'):
            await manager.press_digits(call_id, DigitsRequest(digits='2', reason='Service'))
    asyncio.run(exercise())
    record = manager.db.get(call_id)
    assert record['status'] == 'completed'
    assert record['keypad_events'][0]['status'] == 'unconfirmed'


def test_keypad_rejects_nonconnected(service):
    client, manager, gateway = service
    cid = create(client).json()['call_id']
    assert client.post(f'/calls/{cid}/digits', json={'digits':'2','reason':'Service'}).status_code == 409


def test_mcp_creation_and_retrieval(service):
    _, manager, _ = service
    manager.settings.oauth_database_path = manager.db.path.parent / 'oauth.db'
    mcp, _ = create_mcp(manager)
    async def exercise():
        names = {t.name for t in await mcp.list_tools()}
        assert names == {'place_call','get_call_status','get_call_result','press_digits','hangup_call',
                         'subscribe_call_events','get_call_event_subscriptions','unsubscribe_call_events','send_call_update'}
        result = await mcp.call_tool('place_call', {'phone_number': '+12025550101', 'objective': 'Check status', 'ivr_mode': True})
        structured = json.loads(result.content[0].text)
        cid = structured['call_id']
        record = json.loads((await mcp.call_tool('get_call_result', {'call_id':cid})).content[0].text)
        assert record['objective'] == 'Check status' and record['ivr_mode']
        assert record['voice_tool_operations'] == []
    asyncio.run(exercise())


def test_remote_mcp_requires_owner_oauth(tmp_path):
    settings = Settings(_env_file=None, mcp_enabled=True, public_base_url='https://example.test',
                        database_path=tmp_path/'calls.db', oauth_database_path=tmp_path/'oauth.db')
    app = create_app(settings, gateway=MockTwilio())
    with TestClient(app) as client:
        assert client.post('/mcp', json={}).status_code == 401
        assert client.post('/mcp', json={}, headers={'Authorization':'Bearer invalid'}).status_code == 401
        meta = client.get('/.well-known/oauth-authorization-server').json()
        assert meta['code_challenge_methods_supported'] == ['S256']
        assert meta['scopes_supported'] == ['phone.calls']


def test_pairing_single_use_and_refresh_audience(tmp_path):
    provider = PairingOAuthProvider(tmp_path/'oauth.db', 'https://example.test')
    code = provider.create_pairing_code()
    assert not provider._consume_pairing_code('wrong')
    assert provider._consume_pairing_code(code)
    assert not provider._consume_pairing_code(code)
    async def exercise():
        tokens = provider._issue_tokens('client', ['phone.calls'], 'https://example.test/mcp')
        access = await provider.load_access_token(tokens.access_token)
        assert access.resource == 'https://example.test/mcp'
        refresh = await provider.load_refresh_token(None, tokens.refresh_token)
        client = OAuthClientInformationFull(client_id='client',redirect_uris=['https://chatgpt.com/callback'])
        rotated = await provider.exchange_refresh_token(client, refresh, ['phone.calls'])
        assert (await provider.load_access_token(rotated.access_token)).resource == access.resource
        assert await provider.load_refresh_token(None, tokens.refresh_token) is None
        bad = provider._issue_tokens('client', ['phone.calls'], 'https://wrong.test/mcp')
        assert await provider.load_access_token(bad.access_token) is None
    asyncio.run(exercise())
