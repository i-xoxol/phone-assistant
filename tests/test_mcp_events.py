import asyncio
import base64
import hashlib
import hmac
import json
import socket
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate
from mcp.shared.exceptions import MCPError
from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from app.config import Settings
from app.main import create_app
from app.mcp.callback import CallbackError, PublicHTTPSConnection, callback_url, signed_headers, validate_secret
from app.mcp.events import PhoneEvents, Subscribe, VERSION, definitions
from app.oauth_provider import PairingOAuthProvider
from app.storage.models import CallRequest
from tests.test_calls import MockTwilio, service

SECRET = 'whsec_' + base64.b64encode(b'x' * 32).decode()
OTHER_SECRET = 'whsec_' + base64.b64encode(b'y' * 32).decode()


@pytest.fixture
def events(service, monkeypatch):
    _, manager, _ = service
    provider = PairingOAuthProvider(manager.db.path.parent / 'oauth.db', 'https://example.test')
    tokens = provider._issue_tokens('test-client', ['phone.calls'], 'https://example.test/mcp')
    token = asyncio.run(provider.load_access_token(tokens.access_token))
    monkeypatch.setattr('app.mcp.events.get_access_token', lambda: token)
    received = []
    async def receiver(url, body, headers):
        event = Webhook(SECRET).verify(body, headers)
        received.append((event, headers))
        return (200, json.dumps({'challenge': event['challenge']}).encode()) if event.get('type') == 'verification' else (204, b'')
    bridge = PhoneEvents(manager, provider, sender=receiver)
    cid = manager.db.create(CallRequest(phone_number='+12025550101', objective='Test'))
    params = Subscribe(name='transcript.delta', arguments={'call_id': cid},
        delivery={'mode': 'webhook', 'url': 'https://callback.example.com/events', 'secret': SECRET})
    return bridge, params, received, token


def delta(bridge, cid, eid='fragment'):
    bridge.manager.db.transcript(cid, {'event_id': eid, 'type': 'session.input_transcript.delta',
        'start_ms': 10, 'end_ms': 100, 'delta': 'Test caption'})
    return bridge.manager.db.events_after(0)[-1]


def test_subscription_idempotence_verification_signatures_and_persistence(events):
    bridge, params, received, _ = events
    async def exercise():
        first = await bridge.subscribe(None, params)
        second = await bridge.subscribe(None, params)
        assert first['id'] == second['id'] and first['cursor'] is None
        assert len(received) == 1  # Bounded verification cache.
        event = delta(bridge, str(params.arguments.call_id))
        await bridge.deliver(first['id'], event)
        delivered, headers = received[-1]
        assert delivered['name'] == 'transcript.delta'
        assert delivered['eventId'] == headers['webhook-id']
        validate(delivered['data'], definitions()[0]['payloadSchema'])
        assert bridge.get(first['id'])['cursor'] == event['id']
        report = bridge.list_subscriptions(str(params.arguments.call_id))['subscriptions'][0]
        assert report['delivered_events'] == 1 and report['last_delivery_at']
        bridge.ack(first['id'], event['id'], delivered=True)
        assert bridge.get(first['id'])['delivered_events'] == 1
        restarted = PhoneEvents(bridge.manager, bridge.provider)
        assert restarted.get(first['id'])['cursor'] == event['id']
        assert restarted.get(first['id'])['delivered_events'] == 1
        await bridge.unsubscribe(None, params.model_copy(update={'delivery': params.delivery.model_copy(update={'secret': None})}))
        await bridge.unsubscribe(None, params)
        assert not bridge.get(first['id'])['active'] and bridge.get(first['id'])['secret'] == ''
        before = len(received)
        await bridge.deliver(first['id'], event)
        assert len(received) == before
    asyncio.run(exercise())


def test_worker_filtering_push_without_polling(events):
    bridge, params, received, _ = events
    async def exercise():
        sid = (await bridge.subscribe(None, params))['id']
        other = bridge.manager.db.create(CallRequest(phone_number='+12025550102', objective='Other'))
        delta(bridge, other, 'other-call')
        bridge.manager.db.transition(str(params.arguments.call_id), 'in_progress')
        task = asyncio.create_task(bridge.worker(sid))
        try:
            await asyncio.sleep(0)
            assert len(received) == 1
            delta(bridge, str(params.arguments.call_id))
            async with asyncio.timeout(.5):
                while len(received) < 2:
                    await asyncio.sleep(.01)
            assert len(received) == 2 and received[-1][0]['data']['call_id'] == str(params.arguments.call_id)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_failed_challenge_does_not_create_subscription(events):
    bridge, params, received, _ = events
    async def wrong(*args): return 200, b'{"challenge":"wrong"}'
    bridge.sender = wrong
    with pytest.raises(MCPError) as error:
        asyncio.run(bridge.subscribe(None, params))
    assert error.value.code == -32015 and error.value.data['reason'] == 'challenge_failed'
    owner, _ = bridge.principal()
    assert bridge.get(bridge.identity(params, owner)[0]) is None


def test_native_callback_blocked_during_verification(events):
    bridge, params, _, _ = events
    async def blocked(*args): raise CallbackError('blocked_address')
    bridge.sender = blocked
    with pytest.raises(MCPError) as error:
        asyncio.run(bridge.subscribe(None, params))
    assert error.value.data['reason'] == 'blocked_address'


def test_expiry_refresh_rotation_and_disconnect(events):
    bridge, params, received, token = events
    async def receiver(url, body, headers):
        value = Webhook(OTHER_SECRET).verify(body, headers)
        received.append((value, headers))
        return 200, json.dumps({'challenge': value.get('challenge')}).encode()
    async def exercise():
        sid = (await bridge.subscribe(None, params))['id']
        event = delta(bridge, str(params.arguments.call_id))
        bridge.sender = receiver
        rotated = params.model_copy(update={'delivery': params.delivery.model_copy(update={'secret': OTHER_SECRET})})
        assert (await bridge.subscribe(None, rotated))['id'] == sid
        await bridge.deliver(sid, event)
        assert len(received[-1][1]['webhook-signature'].split(' ')) == 2
        assert Webhook(SECRET).verify(json.dumps(received[-1][0], ensure_ascii=False, separators=(',', ':')),
                                    received[-1][1]) == received[-1][0]
        with bridge.manager.db.connect() as db:
            db.execute('UPDATE mcp_event_subscriptions SET expires_at=? WHERE id=?', (time.time()-1, sid))
        before = len(received)
        await bridge.deliver(sid, event)
        assert len(received) == before
        await bridge.subscribe(None, rotated)
        assert bridge.get(sid)['cursor'] == bridge.manager.db.event_head()
        await bridge.provider.revoke_token(token)
        assert not bridge.provider.connection_active(token.client_id)
        await bridge.worker(sid)
        assert not bridge.get(sid)['active']
    asyncio.run(exercise())


@pytest.mark.parametrize('response', [503, 429, 410, 413])
def test_retry_identity_bounded_attempts_and_permanent_responses(events, response):
    bridge, params, received, _ = events
    async def exercise():
        sid = (await bridge.subscribe(None, params))['id']
        event = delta(bridge, str(params.arguments.call_id))
        attempts = []
        async def fail(url, body, headers):
            attempts.append((json.loads(body), headers))
            return response, b''
        bridge.sender = fail
        await bridge.deliver(sid, event)
        record = bridge.get(sid)
        assert record['delivered_events'] == 0 and record['last_delivery_at'] is None
        if response in (503, 429):
            assert record['cursor'] < event['id'] and record['failures'] == 1 and record['next_attempt'] > time.time()
            for _ in range(7): await bridge.deliver(sid, event)
            assert bridge.get(sid)['cursor'] == event['id']
            assert len({v[0]['eventId'] for v in attempts}) == 1
        elif response == 410:
            assert not record['active']
        else:
            assert record['cursor'] == event['id']
    asyncio.run(exercise())


@pytest.mark.parametrize('value', ['https://u:p@example.com/x', 'http://example.com', 'https://example.com/#x', 'https://example.com/\r\nx'])
def test_invalid_callback_urls(value):
    with pytest.raises(CallbackError): callback_url(value)


@pytest.mark.parametrize('value', ['', 'secret', 'whsec_invalid', 'whsec_' + base64.b64encode(b'x').decode()])
def test_invalid_signing_keys(value):
    with pytest.raises(ValueError): validate_secret(value)


def test_standard_webhooks_exact_bytes_timestamp_id_and_tamper():
    body = '{"text":"Привіт"}'.encode()
    headers = signed_headers('sub', 'evt', SECRET, body)
    expected = base64.b64encode(hmac.new(b'x'*32,
        b'evt.' + headers['webhook-timestamp'].encode() + b'.' + body, hashlib.sha256).digest()).decode()
    assert headers['webhook-signature'] == 'v1,' + expected
    assert Webhook(SECRET).verify(body, headers)['text'] == 'Привіт'
    with pytest.raises(WebhookVerificationError): Webhook(SECRET).verify(body + b' ', headers)
    with pytest.raises(WebhookVerificationError): Webhook(SECRET).verify(body, {**headers, 'webhook-id': 'changed'})


def test_dns_validation_and_connection_pinning(monkeypatch):
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: addresses)
    connected = []
    class Context:
        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == 'receiver.example.com'
            return sock
    monkeypatch.setattr(socket, 'create_connection', lambda address, **k: connected.append(address) or object())
    connection = PublicHTTPSConnection('receiver.example.com')
    connection._context = Context()
    with pytest.raises(CallbackError): connection.connect()
    assert not connected
    addresses[:] = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
    connection.connect()
    assert connected == [('8.8.8.8', 443)]
    # Even a mixed public/private DNS answer is refused.
    addresses.append((socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', 443, 0, 0)))
    with pytest.raises(CallbackError): connection.connect()


def test_authenticated_mcp2_discovery_events_and_legacy_tools(tmp_path):
    settings = Settings(_env_file=None, mcp_enabled=True, public_base_url='https://example.test',
        database_path=tmp_path/'calls.db', oauth_database_path=tmp_path/'oauth.db')
    app = create_app(settings, gateway=MockTwilio())
    provider = app.state.oauth
    tokens = provider._issue_tokens('http-test', ['phone.calls'], 'https://example.test/mcp')
    with TestClient(app) as client:
        def rpc(method, params=None, authorized=True):
            headers = {'Accept': 'application/json, text/event-stream',
                'MCP-Protocol-Version': VERSION, 'MCP-Method': method}
            if method == 'tools/call':
                headers['MCP-Name'] = params['name']
            if authorized: headers['Authorization'] = 'Bearer ' + tokens.access_token
            meta = {'io.modelcontextprotocol/protocolVersion': VERSION,
                'io.modelcontextprotocol/clientInfo': {'name': 'test', 'version': '1'},
                'io.modelcontextprotocol/clientCapabilities': {}}
            return client.post('/mcp', headers=headers, json={'jsonrpc': '2.0', 'id': 1,
                'method': method, 'params': {**(params or {}), '_meta': meta}})
        assert rpc('events/list', authorized=False).status_code == 401
        discover = rpc('server/discover').json()
        assert discover['result']['supportedVersions'] == [VERSION]
        assert discover['result']['capabilities']['events'] == {}
        assert [e['name'] for e in rpc('events/list').json()['result']['events']] == ['transcript.delta', 'call.status']
        assert len(rpc('tools/list').json()['result']['tools']) == 9
        cid = app.state.manager.db.create(CallRequest(phone_number='+12025550101', objective='Protocol test'))
        challenges = []
        async def receiver(url, body, headers):
            value = Webhook(SECRET).verify(body, headers)
            challenges.append(value)
            return 200, json.dumps({'challenge': value['challenge']}).encode()
        app.state.manager.mcp_events.sender = receiver
        params = {'name':'transcript.delta','arguments':{'call_id':cid},
            'delivery':{'mode':'webhook','url':'https://callback.example.com/events','secret':SECRET}}
        subscription = rpc('events/subscribe', params).json()['result']
        assert subscription['id'].startswith('sub_') and len(challenges)==1
        params['delivery'].pop('secret')
        assert rpc('events/unsubscribe', params).json()['result']['resultType']=='complete'
        assert not app.state.manager.mcp_events.get(subscription['id'])['active']
        # Ordinary tools use the same verified, owner-scoped subscription service.
        def tool(name, arguments):
            wire = rpc('tools/call', {'name':name, 'arguments':arguments}).json()
            assert 'result' in wire, wire
            result = wire['result']
            if 'structuredContent' not in result and result.get('content'):
                try:
                    result['structuredContent'] = json.loads(result['content'][0]['text'])
                except (ValueError, KeyError):
                    pass
            return result
        update = tool('send_call_update', {'call_id':cid, 'content':'Thursday afternoon is available.'})['structuredContent']
        assert update['status'] == 'queued'
        record = tool('get_call_result', {'call_id':cid})['structuredContent']
        assert record['call_updates'][0]['update_id'] == update['update_id']
        subscribe = tool('subscribe_call_events', {'call_id':cid,
            'callback_url':'https://callback.example.com/events','signing_secret':SECRET})
        assert not subscribe.get('isError')
        subscribed = subscribe['structuredContent']
        assert subscribed['callback_verified']
        read = tool('get_call_event_subscriptions', {'call_id':cid})['structuredContent']
        assert not read['subscription_required']
        assert read['subscriptions'][0]['active'] and read['subscriptions'][0]['delivered_events']==0
        assert SECRET not in json.dumps(read)
        stopped = tool('unsubscribe_call_events', {'subscription_id':subscribed['id']})['structuredContent']
        assert not stopped['active']
        assert not tool('unsubscribe_call_events', {'subscription_id':subscribed['id']})['structuredContent']['active']
        assert tool('get_call_event_subscriptions', {'call_id':cid})['structuredContent']['subscription_required']
        denied = rpc('tools/call', {'name':'subscribe_call_events', 'arguments':{
            'call_id':cid, 'callback_url':'http://callback.example.com/events',
            'signing_secret':SECRET}}).json()
        assert denied['error']['code'] == -32015
        assert denied['error']['data']['reason'] == 'invalid_url'
        wrong = provider._issue_tokens('other', ['phone.calls'], 'https://wrong.test/mcp')
        assert client.post('/mcp', headers={'Authorization': 'Bearer ' + wrong.access_token}, json={}).status_code == 401
        # Preserve the previous initialize protocol used by existing MCP clients.
        response = client.post('/mcp', headers={'Accept':'application/json, text/event-stream',
            'Authorization':'Bearer '+tokens.access_token}, json={'jsonrpc':'2.0','id':2,'method':'initialize',
            'params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'legacy','version':'1'}}})
        assert response.json()['result']['protocolVersion'] == '2025-11-25'
