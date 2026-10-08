import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.config import Settings
from app.main import create_app
from app.storage.database import Database
from app.storage.models import CallRequest


class MockTwilio:
    def __init__(self):
        self.calls = []
        self.failure = False
        self.status = 'queued'

    async def place(self, call_id, number, minutes):
        if self.failure:
            raise RuntimeError('secret-token-must-not-leak')
        self.calls.append((call_id, number, minutes))
        return SimpleNamespace(sid='CA123', status='queued')

    async def fetch(self, sid):
        return SimpleNamespace(sid=sid, status=self.status, duration='10')

    async def hangup(self, sid):
        self.status = 'canceled' if self.status in ('queued', 'initiated', 'ringing') else 'completed'
        return await self.fetch(sid)


@pytest.fixture
def service(tmp_path):
    settings = Settings(_env_file=None, openai_api_key='fake', twilio_account_sid='AC123',
                        twilio_auth_token='twilio-test', twilio_phone_number='+12025550100',
                        public_base_url='https://example.test', local_api_token='local-test',
                        database_path=tmp_path / 'calls.sqlite3')
    gateway = MockTwilio()
    app = create_app(settings, gateway=gateway)
    with TestClient(app) as client:
        client.headers['Authorization'] = 'Bearer local-test'
        yield client, app.state.manager, gateway


def create(client):
    return client.post('/calls', json={'phone_number': '+12025550101', 'objective': 'Check status'})


def callback(client, manager, call_id, status, sequence=0, **extra):
    path = f'/twilio/status/{call_id}'
    data = {'AccountSid': 'AC123', 'CallSid': 'CA123', 'CallStatus': status,
            'SequenceNumber': str(sequence), **extra}
    signature = RequestValidator('twilio-test').compute_signature('https://example.test' + path, data)
    return client.post(path, data=data, headers={'X-Twilio-Signature': signature})


def test_call_creation_and_voice_twiml(service):
    client, manager, gateway = service
    response = create(client)
    assert response.status_code == 201
    result = response.json()
    assert result['status'] == 'queued'
    assert len(gateway.calls) == 1
    call_id = result['call_id']
    path = f'/twilio/voice/{call_id}'
    data = {'AccountSid': 'AC123', 'CallSid': 'CA123'}
    signature = RequestValidator('twilio-test').compute_signature('https://example.test' + path, data)
    voice = client.post(path, data=data, headers={'X-Twilio-Signature': signature})
    assert voice.status_code == 200
    assert '<Connect><Stream' in voice.text
    assert f'wss://example.test/twilio/media/{call_id}' in voice.text
    assert f'statusCallback="https://example.test/twilio/stream-status/{call_id}"' in voice.text


@pytest.mark.parametrize('number', ['2035551234', '+0123456789', 'abc', '+1', '+1234567890123456', '+1١٢٣٤٥٦٧٨٩'])
def test_invalid_numbers(service, number):
    client, _, gateway = service
    assert client.post('/calls', json={'phone_number': number, 'objective': 'Test'}).status_code == 422
    assert not gateway.calls


def test_twilio_failure_is_persisted_and_sanitized(service):
    client, manager, gateway = service
    gateway.failure = True
    result = create(client).json()
    record = manager.db.get(result['call_id'])
    assert result['status'] == 'failed'
    assert record['completed_at']
    assert 'secret-token' not in record['error_message']


def test_no_answer(service):
    client, manager, _ = service
    call_id = create(client).json()['call_id']
    assert callback(client, manager, call_id, 'no-answer').status_code == 204
    assert client.get(f'/calls/{call_id}').json()['status'] == 'no_answer'


def test_completion_persistence_and_out_of_order_callback(service):
    client, manager, _ = service
    call_id = create(client).json()['call_id']
    assert callback(client, manager, call_id, 'in-progress', 2).status_code == 204
    assert callback(client, manager, call_id, 'ringing', 1).status_code == 204
    assert manager.status(call_id)['status'] == 'in_progress'
    assert callback(client, manager, call_id, 'completed', 3, CallDuration='183').status_code == 204
    assert callback(client, manager, call_id, 'in-progress', 2).status_code == 204
    record = Database(manager.db.path).get(call_id)
    assert record['status'] == 'completed'
    assert record['duration_seconds'] == 183
    assert record['started_at'] and record['completed_at']
    assert client.get(f'/calls/{call_id}/result').json()['status'] == 'completed'


def test_authentication_and_callback_binding(service):
    client, manager, gateway = service
    assert client.post('/calls', json={}, headers={'Authorization': 'Bearer bad'}).status_code == 401
    call_id = create(client).json()['call_id']
    assert client.post(f'/twilio/status/{call_id}', data={}).status_code == 403
    assert callback(client, manager, call_id, 'completed', CallSid='CAdifferent').status_code == 403
    assert manager.status(call_id)['status'] == 'queued'


def test_hangup_and_refresh(service):
    client, manager, gateway = service
    call_id = create(client).json()['call_id']
    gateway.status = 'ringing'
    assert client.get(f'/calls/{call_id}?refresh=true').json()['status'] == 'ringing'
    assert client.post(f'/calls/{call_id}/hangup').json()['status'] == 'cancelled'


def test_missing_configuration(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / 'db')
    app = create_app(settings, gateway=MockTwilio())
    assert TestClient(app).get('/health').json()['ready'] is False


def test_gateway_creation_sets_callbacks_and_provider_time_limit(service, monkeypatch):
    from app.calling.twilio_client import TwilioGateway
    _, manager, _ = service
    arguments = {}

    def capture(**kwargs):
        arguments.update(kwargs)
        return SimpleNamespace(sid='CA123', status='queued')

    gateway = TwilioGateway(manager.settings)
    monkeypatch.setattr(gateway, 'client', lambda: SimpleNamespace(calls=SimpleNamespace(create=capture)))
    asyncio.run(gateway.place('test-id', '+12025550101', 2))
    assert arguments['time_limit'] == 120
    assert arguments['record'] is False
    assert arguments['status_callback_event'] == ['initiated', 'ringing', 'answered', 'completed']
    assert arguments['url'] == 'https://example.test/twilio/voice/test-id'
    assert arguments['status_callback'] == 'https://example.test/twilio/status/test-id'


def test_stream_failure_callback_is_bound_sanitized_and_persistent(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    path = f'/twilio/stream-status/{cid}'
    form = {'AccountSid':'AC123', 'CallSid':'CA123', 'StreamSid':'MZtest123',
            'StreamEvent':'stream-error', 'StreamError':'31903 Broken pipe private-token https://private.example/'}
    def send(data):
        signature = RequestValidator('twilio-test').compute_signature('https://example.test'+path, data)
        return client.post(path, data=data, headers={'X-Twilio-Signature':signature})
    assert client.post(path, data=form).status_code == 403
    assert send({**form,'CallSid':'CAother'}).status_code == 403
    assert send({**form,'StreamEvent':'bad'}).status_code == 400
    assert callback(client, manager, cid, 'completed', 3, CallDuration='50').status_code == 204
    assert send(form).status_code == 204
    manager.db.voice_error(cid, 'Later cleanup error', {'reason':'live_transport_closed_without_finalization'})
    result = client.get(f'/calls/{cid}/result').json()
    assert result['status'] == 'completed' and result['duration_seconds'] == 50
    assert result['error_message'] == 'Twilio audio stream failed: connection broken pipe (31903)'
    assert result['voice_events'][0]['error_code'] == '31903'
    assert 'private-token' not in str(result) and 'private.example' not in str(result)
    saved = Database(manager.db.path).get(cid)
    assert saved['voice_events'] == result['voice_events']


@pytest.mark.parametrize('change', ['navigation','stopping','superseded_stream'])
def test_intentional_stream_changes_are_not_reported_as_call_failures(service, change):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    if change == 'navigation':
        manager.navigation[cid] = asyncio.Event()
    elif change == 'stopping':
        manager.stopping.add(cid)
    else:
        manager.bridges[cid] = SimpleNamespace(stream_sid='MZnew', handoff=False, end_requested=asyncio.Event())
    path = f'/twilio/stream-status/{cid}'
    data = {'AccountSid':'AC123','CallSid':'CA123','StreamSid':'MZold',
            'StreamEvent':'stream-error','StreamError':'31903 Broken pipe'}
    signature = RequestValidator('twilio-test').compute_signature('https://example.test'+path, data)
    assert client.post(path, data=data, headers={'X-Twilio-Signature':signature}).status_code == 204
    record = manager.db.get(cid)
    assert record['error_message'] is None
    assert record['voice_events'][0]['expected_disconnect']
