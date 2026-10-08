import asyncio
import base64
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from app.calling.live_session import MediaBridge, BridgeFailure, valid_stream_start, start_session, has_speech
from app.calling.prompts import session_config, voice_prompt
from app.storage.models import CallRequest
from tests.test_calls import create, service, callback


class Event:
    def __init__(self, **data):
        self.data = data
        self.type = data['type']

    def model_dump(self):
        return self.data


class FakeConnection:
    def __init__(self):
        self.events = asyncio.Queue()
        self.config = None
        self.audio = []
        self.greeting = None
        self.session = SimpleNamespace(
            start=self.start, close=self.close,
            input_audio=SimpleNamespace(append=self.append_audio),
            instructions=SimpleNamespace(append=self.instructions),
            commentary=SimpleNamespace(append=self.commentary))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()

    async def start(self, session):
        self.config = session
        await self.events.put(Event(type='session.started', session={'id': 'live_test'}))

    async def instructions(self, **kwargs):
        self.greeting = kwargs

    async def commentary(self, **kwargs):
        pass

    async def append_audio(self, audio):
        self.audio.append(audio)
        await self.events.put(Event(type='session.input_transcript.delta', event_id='in_1',
                                    delta='Hello.', start_ms=1000, end_ms=1500))
        await self.events.put(Event(type='session.output_transcript.delta', event_id='out_1',
                                    delta='I am an AI assistant.', start_ms=1600, end_ms=2000))
        await self.events.put(Event(type='session.output_audio.delta', delta=audio))

    async def close(self):
        await self.events.put(Event(type='session.closed'))


class FakeClient:
    def __init__(self, connection):
        self.live = SimpleNamespace(connect=lambda: connection)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def stream_headers(path, scheme='https', suffix=''):
    signature = RequestValidator('twilio-test').compute_signature(scheme + '://example.test' + path + suffix, {})
    return {'X-Twilio-Signature': signature}


def start_message():
    return {'event': 'start', 'streamSid': 'MZ123', 'start': {
        'accountSid': 'AC123', 'callSid': 'CA123', 'streamSid': 'MZ123',
        'mediaFormat': {'encoding': 'audio/x-mulaw', 'sampleRate': 8000, 'channels': 1}}}


@pytest.mark.parametrize('scheme,suffix', [('https', ''), ('wss', ''), ('wss', '/')])
def test_live_audio_transcript_and_graceful_close(service, monkeypatch, scheme, suffix):
    client, manager, gateway = service
    call_id = create(client).json()['call_id']
    connection = FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    path = f'/twilio/media/{call_id}'
    payload = base64.b64encode(bytes([255]) * 160).decode()
    with client.websocket_connect(path, headers=stream_headers(path, scheme, suffix)) as ws:
        ws.send_json({'event': 'connected'})
        ws.send_json(start_message())
        ws.send_json({'event': 'media', 'streamSid': 'MZ123',
                      'media': {'track': 'inbound', 'payload': payload}})
        audio = ws.receive_json()
        mark = ws.receive_json()
        assert audio['media']['payload'] == payload
        assert mark['event'] == 'mark'
        ws.send_json(mark)
        ws.send_json({'event': 'stop', 'streamSid': 'MZ123'})
        assert ws.receive_json()['event'] == 'clear'
        # Wait for the server to finish cleanup, rather than racing SQLite reads.
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    record = manager.db.get(call_id)
    assert record['openai_session_id'] == 'live_test'
    assert record['voice_finalized'] == 1
    assert len(record['transcript']) == 2
    assert record['transcript'][0]['speaker'] == 'callee'
    assert record['transcript'][0]['timestamp'] == 1
    assert connection.config['model'] == 'gpt-live-1'
    assert connection.config['audio']['format'] == {'type': 'audio/pcmu', 'rate': 8000}
    assert connection.greeting['delegation_id'] is None
    assert connection.audio == [payload]
    callback(client, manager, call_id, 'completed', 3, CallDuration='2')
    assert client.get(f'/calls/{call_id}/result').json()['status'] == 'completed'


def test_websocket_signature_required(service):
    client, manager, _ = service
    call_id = create(client).json()['call_id']
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f'/twilio/media/{call_id}'):
            pass
    # A valid signature for a different route must not authorize this route.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f'/twilio/media/{call_id}',
                                      headers=stream_headers('/wrong-route', 'wss')):
            pass


def test_prompt_contains_constraints_and_disclosure(service):
    _, manager, _ = service
    record = {'recipient': 'Garage', 'objective': 'Check repair', 'context': '2022 Elantra',
              'constraints': ['Do not approve a repair', 'Ask Alex before decisions']}
    prompt = voice_prompt(manager.settings, record)
    for required in ('Garage', 'Check repair', '2022 Elantra', 'Do not approve a repair',
                     'Ask Alex before decisions', 'must NOT authorize payments',
                     'AI personal assistant', 'Never invent', 'SSNs'):
        assert required in prompt
    assert 'callback_number' not in prompt
    manager.settings.callback_number = '+12025550101'
    assert '+12025550101' in voice_prompt(manager.settings, record)


def test_stream_is_bound_to_call_and_codec(service):
    _, manager, _ = service
    record = {'twilio_call_sid': 'CA123'}
    message = start_message()['start']
    assert valid_stream_start(manager.settings, record, message)
    message['callSid'] = 'CAother'
    assert not valid_stream_start(manager.settings, record, message)
    message['callSid'] = 'CA123'
    message['mediaFormat']['sampleRate'] = 24000
    assert not valid_stream_start(manager.settings, record, message)


def test_live_initialization_failure_ends_call_and_persists_error(service, monkeypatch):
    client, manager, gateway = service
    call_id = create(client).json()['call_id']
    connection = FakeConnection()

    async def fail_start(session):
        await connection.events.put(Event(type='error', error={'message': 'do-not-log-this-secret'}))

    connection.session.start = fail_start
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    path = f'/twilio/media/{call_id}'
    with client.websocket_connect(path, headers=stream_headers(path)) as ws:
        ws.send_json({'event': 'connected'})
        ws.send_json(start_message())
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    record = manager.db.get(call_id)
    assert record['status'] == 'failed'
    assert gateway.status == 'canceled'
    assert 'do-not-log' not in record['error_message']


def test_stalled_playback_fails_closed_with_specific_reason(service, monkeypatch):
    _, manager, _ = service
    call_id = manager.db.create(CallRequest(
        phone_number='+12025550101', objective='Test'))
    connection = FakeConnection()
    oversized = base64.b64encode(bytes([255]) * 24001).decode()
    asyncio.run(connection.events.put(Event(type='session.output_audio.delta', delta=oversized)))
    class Socket:
        async def send_json(self, message):
            pass  # No playback acknowledgments: genuinely stalled.
    monkeypatch.setattr('app.calling.live_session.PLAYBACK_STALL_SECONDS', .02)
    bridge = MediaBridge(Socket(), connection, manager, call_id, 'MZ123')
    with pytest.raises(BridgeFailure, match='twilio_playback_stalled'):
        asyncio.run(bridge.receive_live())
    assert sum(bridge.pending_marks.values()) <= 24000


def test_audio_burst_waits_for_playback_and_preserves_audio(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    payload = bytes(range(256)) * 200  # More than six seconds in one provider delta.
    async def exercise():
        connection = FakeConnection()
        class Socket:
            def __init__(self):
                self.incoming = asyncio.Queue()
                self.audio = bytearray()
                self.maximum_pending = 0
            async def send_json(self, message):
                self.maximum_pending = max(self.maximum_pending, sum(bridge.pending_marks.values()))
                if message['event'] == 'media':
                    self.audio.extend(base64.b64decode(message['media']['payload']))
                elif message['event'] == 'mark':
                    # Let the producer fill its queue before playback progresses.
                    await self.incoming.put(message)
            async def receive_json(self):
                await asyncio.sleep(.001)
                return await self.incoming.get()
        socket = Socket()
        bridge = MediaBridge(socket, connection, manager, cid, 'MZ123')
        receiver = asyncio.create_task(bridge.receive_twilio())
        try:
            await asyncio.wait_for(bridge.send_live_audio(payload), 1)
            assert bytes(socket.audio) == payload
            assert socket.maximum_pending <= 24000
            assert bridge.mark_index > 30
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    asyncio.run(exercise())


def test_compaction_advisory_does_not_block_live_only_key():
    connection = FakeConnection()
    message = ('History compaction is disabled for this session because the API key '
               'is missing api.responses.write. The session will continue using recent history.')
    connection.events.put_nowait(Event(type='error', error={'code':'missing_scope', 'message':message}))
    result = asyncio.run(start_session(connection, {'model':'gpt-live-1'}))
    assert result['session']['id'] == 'live_test'


def test_missing_live_scope_remains_fatal():
    connection = FakeConnection()
    connection.events.put_nowait(Event(type='error', error={
        'code':'missing_scope', 'message':'The API key is missing api.live.write.'}))
    with pytest.raises(BridgeFailure, match='live_initialization_rejected'):
        asyncio.run(start_session(connection, {'model':'gpt-live-1'}))


@pytest.mark.parametrize('farewell', ['Thank you. Goodbye.', 'Дякую. До побачення.'])
def test_assistant_hangup_waits_for_goodbye_playback(service, monkeypatch, farewell):
    client, manager, gateway = service
    call_id = create(client).json()['call_id']
    gateway.status = 'in-progress'
    connection = FakeConnection()
    goodbye_payload = base64.b64encode(bytes([0]) * 160).decode()

    async def delayed_goodbye():
        # An acknowledgment followed by a pause is not the goodbye.
        await asyncio.sleep(1.2)
        await connection.events.put(Event(type='session.output_audio.delta', delta=goodbye_payload))
        for index, fragment in enumerate((farewell[:5], farewell[5:])):
            await connection.events.put(Event(type='session.output_transcript.delta',
                event_id=f'bye_{index}', delta=fragment, start_ms=2200, end_ms=2800))

    async def request_ending(audio):
        # A recipient repeating the control phrase must not trigger hangup.
        await connection.events.put(Event(type='session.input_transcript.delta',
            event_id='callee_bye', delta=farewell, start_ms=500, end_ms=700))
        await connection.events.put(Event(type='session.output_audio.delta', delta=goodbye_payload))
        await connection.events.put(Event(type='session.output_transcript.delta',
            event_id='ack', delta='Understood.', start_ms=1000, end_ms=1200))
        asyncio.create_task(delayed_goodbye())

    connection.session.input_audio.append = request_ending
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    path = f'/twilio/media/{call_id}'
    with client.websocket_connect(path, headers=stream_headers(path, 'wss')) as ws:
        ws.send_json(start_message())
        ws.send_json({'event':'media', 'streamSid':'MZ123',
                      'media':{'track':'inbound', 'payload':goodbye_payload}})
        assert ws.receive_json()['media']['payload'] == goodbye_payload
        initial_mark = ws.receive_json()
        ws.send_json(initial_mark)
        # A premature hangup barrier here would fail this assertion.
        assert ws.receive_json()['media']['payload'] == goodbye_payload
        audio_mark = ws.receive_json()
        barrier = ws.receive_json()
        assert barrier['mark']['name'] == 'end_call_playback'
        # No recipient stop/hangup is sent. Withholding Twilio's playback ACK
        # must keep the telephone connection alive, even after speech generation.
        assert gateway.status == 'in-progress'
        assert manager.db.get(call_id)['voice_finalized'] == 0
        ws.send_json(audio_mark)
        ws.send_json(barrier)
        assert ws.receive_json()['event'] == 'clear'
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert gateway.status == 'completed'
    assert manager.db.get(call_id)['voice_finalized'] == 1
    callback(client, manager, call_id, 'completed', 3, CallDuration='3')
    assert manager.db.get(call_id)['status'] == 'completed'


def test_mu_law_closing_silence_detection():
    assert not has_speech(b'')
    assert not has_speech(bytes([255, 127]) * 160)
    assert has_speech(bytes([0]) * 160)


@pytest.mark.parametrize('disconnect', [False, True])
def test_finalization_error_does_not_mask_original_disconnect(service, monkeypatch, disconnect):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    monkeypatch.setattr('app.calling.live_session.FINALIZATION_TIMEOUT_SECONDS', .02)
    async def exercise():
        connection = FakeConnection()
        async def failed_close():
            await connection.events.put(Event(type='error', error={'code':'server_error','message':'private payload'}))
        connection.session.close = failed_close
        class Socket:
            async def send_json(self, message):
                pass
            async def receive_json(self):
                if disconnect:
                    raise WebSocketDisconnect(1006)
                return {'event':'stop', 'streamSid':'MZ123'}
        bridge = MediaBridge(Socket(), connection, manager, cid, 'MZ123')
        manager.attach_bridge(cid, bridge)
        if disconnect:
            with pytest.raises(WebSocketDisconnect) as result:
                await bridge.run(1)
            assert result.value.code == 1006
        else:
            await bridge.run(1)
        assert not bridge.finalized.is_set()
    asyncio.run(exercise())


def test_specific_bridge_error_diagnostics_are_sanitized(service, monkeypatch, caplog):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    connection = FakeConnection()
    async def fail_greeting(**kwargs):
        raise RuntimeError('private-key-do-not-log')
    connection.session.instructions.append = fail_greeting
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    path = f'/twilio/media/{cid}'
    with client.websocket_connect(path, headers=stream_headers(path)) as ws:
        ws.send_json(start_message())
        # Cleanup clears the simulated provider buffer first.
        assert ws.receive_json()['event'] == 'clear'
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    record = manager.db.get(cid)
    assert record['status'] == 'failed'
    assert 'unexpected_error' in record['error_message']
    assert 'private-key' not in str(record['voice_events']) + caplog.text
    assert record['voice_events'][0]['locations']
