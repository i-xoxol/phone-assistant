import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.calling.elevenlabs_session import RealtimeConnection, RealtimeMediaBridge, realtime_config, started_elevenlabs_connection
from app.calling.live_session import BridgeFailure, MediaBridge
from app.calling.voice_backends import BackendChoice, VoiceBackends, bridge_connection
from app.mcp.server import create_mcp
from app.storage.models import CallRequest, CallUpdateRequest
from tests.test_calls import service, create


def configure(manager):
    manager.settings.elevenlabs_api_key = SecretStr('test-key')
    manager.settings.elevenlabs_voice_id = 'test-clone'
    manager.settings.elevenlabs_voice_label = 'Test voice'


def inbound(manager):
    return manager.db.create_inbound('CA'+'1'*32, '+12025550102', 5, 10, 'concierge', False)


class Socket:
    def __init__(self):
        self.sent = []
        self.queue = asyncio.Queue()

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def close(self):
        await self.queue.put(None)

    async def recv(self):
        return json.dumps(await self.queue.get())

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.queue.get()
        if data is None: raise StopAsyncIteration
        return json.dumps(data)


def connection(record=None, tools=None):
    return RealtimeConnection(Socket(), {'tools': tools or [],'instructions':'Base authorization policy.'}, record or {})


async def translated(conn, event):
    return [e async for e in conn.translate(event)]


def test_optional_backend_requires_configuration_before_dial(service):
    client, manager, gateway = service
    assert manager.voice_backends.get()['provider'] == 'openai'
    assert not manager.voice_backends.get()['providers'][1]['configured']
    response = client.post('/calls', json={'phone_number': '+12025550101', 'objective': 'Test', 'voice_provider': 'elevenlabs'})
    assert response.status_code == 503 and not gateway.calls
    assert client.put('/voice/backend', json={'provider': 'elevenlabs'}).status_code == 409
    assert client.put('/voice/backend', json={'provider': 'unknown'}).status_code == 422


def test_provider_is_pinned_across_default_changes_and_restart(service):
    client, manager, _ = service
    configure(manager)
    first = create(client).json()['call_id']
    manager.voice_backends.set(BackendChoice(provider='elevenlabs'))
    second = create(client).json()['call_id']
    manager.voice_backends.set(BackendChoice(provider='openai'))
    reloaded = VoiceBackends(manager.db, manager.settings)
    assert reloaded.pin(manager.db.get(first))['provider'] == 'openai'
    selected = reloaded.pin(manager.db.get(second))
    assert selected['provider'] == 'elevenlabs' and selected['voice'] == 'test-clone'
    manager.settings.elevenlabs_voice_id = 'different-clone'
    assert reloaded.pin(manager.db.get(second)) == selected
    assert client.get(f'/calls/{second}/result').json()['voice_backend']['provider'] == 'elevenlabs'


def test_one_call_override_does_not_change_default(service):
    client, manager, _ = service
    configure(manager)
    result = client.post('/calls', json={'phone_number': '+12025550101', 'objective': 'Test', 'voice_provider': 'elevenlabs'}).json()
    assert manager.voice_backends.for_call(result['call_id'])['provider'] == 'elevenlabs'
    assert manager.voice_backends.get()['provider'] == 'openai'


def test_mcp_backend_controls_and_override(service):
    _, manager, _ = service
    configure(manager)
    manager.settings.oauth_database_path = manager.db.path.parent/'oauth.db'
    mcp, _ = create_mcp(manager)
    async def run():
        async def call(name, args):
            result = await mcp.call_tool(name, args)
            assert not result.is_error
            return json.loads(result.content[0].text)
        assert (await call('get_voice_backend', {}))['provider'] == 'openai'
        assert (await call('set_voice_backend', {'provider': 'elevenlabs'}))['provider'] == 'elevenlabs'
        result = await call('place_call', {'phone_number': '+12025550101', 'objective': 'Test', 'voice_provider': 'openai'})
        assert manager.voice_backends.for_call(result['call_id'])['provider'] == 'openai'
    asyncio.run(run())


def test_portal_backend_requires_owner_and_csrf(service):
    client, manager, _ = service
    assert client.put('/live/voice/backend', json={'provider': 'openai'}).status_code == 401
    code = client.app.state.web_auth.create_code()
    # Same session setup used by the mobile monitor, with its real CSRF gate.
    token = client.app.state.web_auth.login(code)
    session = client.app.state.web_auth.session(token)
    client.cookies.set('__Host-phone-owner', token)
    response = client.put('/live/voice/backend', json={'provider': 'openai'}, headers={'Origin': 'https://example.test'})
    # TestClient's http URL does not send a Secure cookie automatically; use an explicit Cookie.
    headers = {'Origin': 'https://example.test', 'Cookie': '__Host-phone-owner='+token}
    assert client.put('/live/voice/backend', json={'provider': 'openai'}, headers=headers).status_code == 403
    headers['X-CSRF-Token'] = session['csrf']
    assert client.put('/live/voice/backend', json={'provider': 'openai'}, headers=headers).status_code == 200


def test_config_retains_policy_and_only_advertised_tools(service):
    _, manager, _ = service
    configure(manager)
    record = manager.voice_record(inbound(manager))
    selection = manager.voice_backends.pin(record, 'elevenlabs')
    config = realtime_config(manager.settings, record, selection)
    assert config['audio']['input']['format']['type'] == 'audio/pcmu'
    assert config['audio']['output']['voice'] == 'test-clone'
    assert 'never impersonate' in config['instructions'].lower()
    assert 'untrusted' in config['instructions']
    assert 'save_message' in {t['name'] for t in config['tools']}
    assert 'press_digits' not in {t['name'] for t in config['tools']}
    assert 'test-key' not in json.dumps(config)


def test_notes_acknowledged_only_by_provider_not_socket_send():
    async def run():
        conn = connection()
        await conn.session.thinking.append(content='New safe fact', event_id='call_update_test')
        assert conn.pending_instructions
        session = conn.socket.sent[0]['session']
        events = await translated(conn, {'type': 'session.updated', 'session': session})
        assert events == [{'type': 'session.thinking.appended', 'client_event_id': 'call_update_test'}]
        assert not await translated(conn, {'type': 'session.updated', 'session': session})
        assert not any(e['type'] == 'response.create' for e in conn.socket.sent)
    asyncio.run(run())


def test_spoken_update_waits_until_current_response_ends_once():
    async def run():
        conn = connection()
        await translated(conn, {'type': 'response.created', 'response': {'id': 'r1'}})
        await conn.session.commentary.append(content='Tell them the new fact')
        assert not any(e['type'] == 'response.create' for e in conn.socket.sent)
        await translated(conn, {'type': 'response.done', 'response': {'id': 'r1', 'status': 'completed', 'output': []}})
        assert sum(e['type'] == 'response.create' for e in conn.socket.sent) == 1
        assert not conn.want_response
    asyncio.run(run())


def test_barge_in_clears_twilio_and_truncates_unheard_audio(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    async def run():
        conn = connection()
        sent = []
        async def send_json(e): sent.append(e)
        bridge = MediaBridge(SimpleNamespace(send_json=send_json), conn, manager, cid, 'MZ_TEST')
        bridge.pending_marks['one'] = 800
        conn.output_item, conn.output_bytes, conn.active_response = 'spoken_item', 1600, 'r1'
        task = asyncio.create_task(bridge.receive_live())
        await conn.socket.queue.put({'type': 'input_audio_buffer.speech_started', 'item_id': 'caller1', 'audio_start_ms': 200})
        await asyncio.sleep(.01)
        assert sent == [{'event': 'clear', 'streamSid': 'MZ_TEST'}]
        assert not bridge.pending_marks
        assert conn.socket.sent[0] == {'type': 'conversation.item.truncate', 'item_id': 'spoken_item', 'content_index': 0, 'audio_end_ms': 100}
        assert not await translated(conn, {'type': 'response.output_audio.delta', 'response_id': 'r1', 'delta': base64.b64encode(b'\xff'*160).decode()})
        await conn.close()
        await task
    asyncio.run(run())


def test_transcript_partials_and_finals_do_not_duplicate():
    async def run():
        conn = connection()
        events = []
        for event in [
            {'type': 'conversation.item.input_audio_transcription.delta', 'item_id': 'in1', 'delta': 'Hello'},
            {'type': 'conversation.item.input_audio_transcription.completed', 'item_id': 'in1', 'transcript': 'Hello there'},
            {'type': 'response.output_audio_transcript.delta', 'item_id': 'out1', 'delta': 'Hi'},
            {'type': 'response.output_audio_transcript.done', 'item_id': 'out1', 'transcript': 'Hi'}]:
            events += await translated(conn, event)
        assert [e['delta'] for e in events] == ['Hello', ' there', 'Hi']
        assert [e['type'] for e in events] == ['session.input_transcript.delta']*2 + ['session.output_transcript.delta']
    asyncio.run(run())


def test_function_execution_uses_existing_permissions_and_duplicate_claim(service):
    _, manager, _ = service
    cid = inbound(manager)
    async def run():
        conn = connection(tools=[{'name':'save_message'}])
        bridge = MediaBridge(None, conn, manager, cid, 'MZ_TEST', luna_booking=True)
        manager.attach_bridge(cid, bridge)
        item = {'type': 'function_call', 'name': 'save_message', 'call_id': 'function1',
                'arguments': json.dumps({'message': 'Test message.', 'caller_name': 'Test', 'callback_number': ''})}
        for raw in [
            {'type': 'response.created', 'response': {'id': 'r1'}},
            {'type': 'response.output_item.done', 'response_id': 'r1', 'item': item},
            {'type': 'response.output_item.done', 'response_id': 'r1', 'item': item},
            {'type': 'response.done', 'response': {'id': 'r1', 'status': 'completed', 'output': [item]}}]:
            for e in await translated(conn, raw): bridge.tools.handle(e)
        await asyncio.gather(*list(manager.tool_tasks))
        record = manager.db.get(cid)
        assert record['message_id']
        assert len(record['voice_tool_operations']) == 1
        assert record['voice_tool_operations'][0]['status'] == 'confirmed'
        result = await bridge.tools.operation(item)
        assert result['status'] == 'already_requested'
        unknown = {'type': 'response.output_item.done', 'response_id': 'r1',
                   'item': {**item, 'name': 'press_digits'}}
        assert not await translated(conn, unknown)
        manager.detach_bridge(cid, bridge)
    asyncio.run(run())


def test_cancelled_response_never_executes_tool(service):
    _, manager, _ = service
    cid = inbound(manager)
    async def run():
        conn = connection(tools=[{'name':'save_message'}])
        bridge = MediaBridge(None, conn, manager, cid, 'MZ_TEST')
        item = {'type':'function_call','name':'save_message','call_id':'tool1','arguments':'{}'}
        for raw in [{'type':'response.created','response':{'id':'r1'}},
                    {'type':'response.output_item.done','response_id':'r1','item':item},
                    {'type':'response.done','response':{'id':'r1','status':'cancelled','output':[item]}}]:
            for e in await translated(conn,raw): bridge.tools.handle(e)
        assert not manager.tool_tasks and not manager.db.get(cid)['voice_tool_operations']
    asyncio.run(run())


def test_provider_setup_waits_for_duration_cap_and_never_retries(service, monkeypatch):
    _, manager, gateway = service
    configure(manager)
    cid = inbound(manager)
    record = manager.db.get(cid)
    selection = manager.voice_backends.pin(record, 'elevenlabs')
    async def fail(*_): raise RuntimeError('private carrier detail')
    gateway.prepare_inbound = fail
    class NoNetwork:
        def __init__(self, **_): raise AssertionError('Network before cap')
    monkeypatch.setattr('app.calling.elevenlabs_session.httpx.AsyncClient', NoNetwork)
    async def run():
        with pytest.raises(BridgeFailure, match='incoming_duration_limit_failed'):
            async with started_elevenlabs_connection(manager.settings, manager, record, selection): pass
    asyncio.run(run())


def test_burst_audio_cannot_block_barge_in(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    async def run():
        conn = connection()
        sent = []
        async def send_json(e): sent.append(e)
        bridge = RealtimeMediaBridge(SimpleNamespace(send_json=send_json),conn,manager,cid,'MZ_TEST')
        receiver = asyncio.create_task(bridge.receive_live())
        pump = asyncio.create_task(bridge.audio_pump())
        try:
            conn.active_response='r1';conn.output_item='out1'
            await conn.socket.queue.put({'type':'response.output_audio.delta','response_id':'r1',
                                         'delta':base64.b64encode(b'\xaa'*80000).decode()})
            # No Twilio playback marks are acknowledged: the writer must block,
            # while the provider reader still processes the caller interruption.
            await asyncio.sleep(.02)
            assert bridge.output_busy and bridge.pending_marks
            await conn.socket.queue.put({'type':'input_audio_buffer.speech_started','item_id':'in1'})
            async with asyncio.timeout(.5):
                while not any(e['event']=='clear' for e in sent): await asyncio.sleep(.001)
            assert not bridge.pending_marks and bridge.queued_bytes == 0
            assert conn.socket.sent[-1]['type'] == 'conversation.item.truncate'
        finally:
            await conn.close();await receiver
            pump.cancel();await asyncio.gather(pump,return_exceptions=True)
    asyncio.run(run())


def test_context_lookup_preserves_acknowledged_owner_updates():
    async def run():
        conn=connection({'call_updates':[{'mode':'context','content':'Picnic is Friday.','status':'acknowledged'},
                                        {'mode':'context','content':'Unconfirmed update.','status':'sent'}]})
        conn.functions['f1']='get_call_context'
        await conn.response.item.create(item={'type':'function_call_output','call_id':'f1','output':'{}'})
        result=json.loads(conn.socket.sent[-1]['item']['output'])
        assert result['owner_live_updates']==[{'mode':'context','content':'Picnic is Friday.'}]
    asyncio.run(run())


def test_initialization_failure_is_sanitized_and_not_retried(service,monkeypatch,capsys):
    _,manager,_=service
    configure(manager);cid=inbound(manager);record=manager.db.get(cid)
    selection=manager.voice_backends.pin(record,'elevenlabs');attempts=[]
    class Client:
        def __init__(self,**_):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*_):pass
        async def post(self,*args,**kwargs):
            attempts.append(1)
            return httpx.Response(401,json={'detail':'private transcript and test-key'})
    monkeypatch.setattr('app.calling.elevenlabs_session.httpx.AsyncClient',Client)
    async def run():
        with pytest.raises(BridgeFailure,match='elevenlabs_initialization_rejected'):
            async with started_elevenlabs_connection(manager.settings,manager,record,selection):pass
    asyncio.run(run())
    assert attempts==[1]
    captured=capsys.readouterr()
    assert 'test-key' not in captured.out+captured.err
