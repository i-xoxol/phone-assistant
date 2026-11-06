"""Browser transport tests use fictional identities and mocked voice providers."""
import asyncio
import base64
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from app.calling.rules import RuleSet
from app.storage.models import CallRequest, DigitsRequest
from tests.test_calls import service
from tests.test_live import FakeClient, FakeConnection
from tests.test_phone_controls import owner_headers
from tests.test_private_portal import portal, sign_in, PRIVATE


def body(**changes):
    return {'request_id': str(uuid4()), 'scenario': 'direct', 'phone_number': '+12025550101', **changes}


def start(client, headers=None, **changes):
    response = client.post('/live/browser-tests', headers=headers or owner_headers(client), json=body(**changes))
    assert response.status_code == 201, response.text
    return response.json()


def no_phone(monkeypatch, gateway):
    async def prohibited(*args, **kwargs):
        pytest.fail('A browser test tried to contact the telephone provider')
    for method in ('place', 'fetch', 'hangup', 'hangup_fast', 'prepare_inbound', 'verified_caller_id',
                   'press_digits', 'redirect_call', 'stop_recording'):
        monkeypatch.setattr(gateway, method, prohibited, raising=False)


def test_start_requires_owner_origin_and_csrf(service):
    client, manager, _ = service
    assert client.post('/live/browser-tests', json=body()).status_code == 401
    headers = owner_headers(client)
    assert client.post('/live/browser-tests', json=body(), headers={**headers, 'Origin': 'https://other.test'}).status_code == 403
    assert client.post('/live/browser-tests', json=body(), headers={**headers, 'X-CSRF-Token': 'bad'}).status_code == 403
    assert not manager.db.recent_calls()


@pytest.mark.parametrize('changes', [
    {'scenario': 'forwarded'}, {'scenario': 'outbound'}, {'phone_number': 'anonymous'},
    {'scenario': 'direct', 'forwarded_from': '+12025550102'}, {'max_duration_minutes': 16},
    {'voice': 'arbitrary'}, {'extra': 'not allowed'}])
def test_invalid_test_is_not_created(service, changes):
    client, manager, _ = service
    assert client.post('/live/browser-tests', headers=owner_headers(client), json=body(**changes)).status_code == 422
    assert not manager.db.recent_calls()


@pytest.mark.parametrize('scenario,number,forwarded,arrival,ambiguous', [
    ('direct', '+12025550101', '', 'direct_or_unreported', False),
    ('direct', '', '', 'direct_or_unreported', False),
    ('forwarded', '+12025550101', '+12025550109', 'forwarded_from_owner_mobile', False),
    ('forwarded', '+12025550101', '+12025550102', 'forwarded_other', False),
    ('forwarded', '+12025550109', '+12025550109', 'forwarded_from_owner_mobile', True),
    ('outbound', '+12025550101', '', 'outbound', False)])
def test_scenarios_and_refresh_never_contact_twilio(service, monkeypatch, scenario, number, forwarded, arrival, ambiguous):
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    manager.settings.owner_transfer_number = '+12025550109'
    result = start(client, scenario=scenario, phone_number=number, forwarded_from=forwarded, objective='Ask for opening hours.')
    cid = result['call_id']
    assert result['direction'] == ('outbound' if scenario == 'outbound' else 'inbound')
    assert result['phone_number'] == number and result['twilio_call_sid'] is None
    assert result['browser_test']['scenario'] == scenario
    assert result['forwarding']['arrival_type'] == arrival
    assert result['forwarding']['caller_identity_ambiguous'] is ambiguous
    record = manager.voice_record(cid)
    assert record['browser_test'] and record['voice_backend'] and record['personality']
    assert asyncio.run(manager.refresh(cid))['status'] == 'ringing'
    assert asyncio.run(manager.hangup_call(cid))['status'] == 'completed'
    assert not gateway.calls


def test_idempotency_and_single_test_admission(service):
    client, manager, _ = service
    headers, request = owner_headers(client), body()
    first = client.post('/live/browser-tests', headers=headers, json=request).json()
    repeat = client.post('/live/browser-tests', headers=headers, json=request)
    assert repeat.status_code == 201 and repeat.json()['call_id'] == first['call_id']
    assert client.post('/live/browser-tests', headers=headers, json=body()).status_code == 409
    assert client.post('/live/browser-tests', headers=headers, json={**request, 'phone_number': ''}).status_code == 409
    assert len(manager.db.recent_calls()) == 1


def test_forwarded_rules_and_explicit_choices_are_pinned(service):
    client, manager, _ = service
    manager.rules.save(RuleSet(revision=manager.rules.get()['revision'], rules=[{
        'name': 'Forwarded office test', 'when': {'forwarded_from': '+12025550102'},
        'then': {'greeting': 'Ask for the purpose of the call.'}}]))
    result = start(client, scenario='forwarded', forwarded_from='+12025550102')
    assert result['routing_rule']['name'] == 'Forwarded office test'
    assert result['routing_rule']['actions']['greeting'] == 'Ask for the purpose of the call.'


def test_browser_tests_do_not_block_incoming_admission_or_enter_recall(service):
    client, manager, _ = service
    result = start(client)
    cid = manager.db.create_inbound('CA_fixture', '+12025550101', 5, 1)
    manager.transition(cid, 'completed')
    manager.transition(result['call_id'], 'completed')
    assert [r['call_id'] for r in manager.memory.records('+12025550101')] == [cid]


@pytest.mark.parametrize('scenario', ['direct', 'forwarded', 'outbound'])
def test_shared_bridge_audio_captions_and_close_without_phone(service, monkeypatch, scenario):
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    connection = FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    headers = owner_headers(client)
    result = start(client, headers, scenario=scenario, objective='Ask about hours.',
        forwarded_from='+12025550102' if scenario == 'forwarded' else '')
    cid = result['call_id']
    payload = base64.b64encode(bytes([255]) * 160).decode()
    with client.websocket_connect('/live/browser-tests/' + cid + '/audio', headers=headers) as ws:
        ws.send_json({'event': 'start', 'csrf': headers['X-CSRF-Token']})
        assert ws.receive_json()['event'] == 'ready'
        ws.send_json({'event': 'media', 'media': {'payload': payload, 'timestamp': 0}})
        received = ws.receive_json()
        assert received['event'] == 'media' and received['media']['payload'] == payload
        mark = ws.receive_json()
        assert mark['event'] == 'mark'
        ws.send_json(mark)
        ws.send_json({'event': 'stop'})
        while True:
            try:
                ws.receive_json()
            except WebSocketDisconnect:
                break
    record = manager.result(cid)
    assert record['status'] == 'completed' and record['voice_finalized']
    assert record['transcript'] and connection.config and connection.audio == [payload]
    assert cid not in manager.bridges
    assert not gateway.calls and not gateway.incoming_preparations


@pytest.mark.parametrize('variant', ['origin', 'session', 'csrf', 'real_call'])
def test_browser_socket_rejects_wrong_binding_without_starting_voice(service, monkeypatch, variant):
    client, manager, _ = service
    connection = FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    headers = owner_headers(client)
    cid = start(client, headers)['call_id']
    if variant == 'origin': headers = {**headers, 'Origin': 'https://other.test'}
    if variant == 'session': headers = owner_headers(client)
    if variant == 'real_call': cid = manager.db.create(CallRequest(phone_number='+12025550101', objective='Fictional call'))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/live/browser-tests/' + cid + '/audio', headers=headers) as ws:
            ws.send_json({'event': 'start', 'csrf': 'bad' if variant == 'csrf' else headers['X-CSRF-Token']})
            while True:
                ws.receive_json()
    assert connection.config is None


def test_bad_audio_fails_safely_and_stops_provider(service, monkeypatch):
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    connection = FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    headers = owner_headers(client)
    cid = start(client, headers)['call_id']
    with client.websocket_connect('/live/browser-tests/' + cid + '/audio', headers=headers) as ws:
        ws.send_json({'event': 'start', 'csrf': headers['X-CSRF-Token']})
        assert ws.receive_json()['event'] == 'ready'
        ws.send_json({'event': 'media', 'media': {'payload': 'not valid base64', 'timestamp': 0}})
        while True:
            try: ws.receive_json()
            except WebSocketDisconnect: break
    assert manager.db.get(cid)['status'] == 'failed'
    assert not connection.audio and cid not in manager.bridges


def test_phone_actions_are_simulated(service, monkeypatch):
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    cid = start(client, scenario='outbound', objective='Test a menu')['call_id']
    connection = FakeConnection()
    from app.calling.live_session import MediaBridge
    class Socket:
        async def send_json(self, data): pass
    bridge = MediaBridge(Socket(), connection, manager, cid, 'browser_fixture')
    manager.transition(cid, 'in_progress')
    manager.attach_bridge(cid, bridge)
    assert asyncio.run(manager.press_digits(cid, DigitsRequest(digits='1#', reason='Test menu')))['keypad_status'] == 'simulated'
    assert asyncio.run(manager.transfer.request(cid, 'Test owner availability'))['simulated']
    assert asyncio.run(manager.recordings.stop(cid))['status'] == 'not_recording'
    assert asyncio.run(manager.hangup_call(cid))['status'] == 'completed'


def test_stale_and_used_socket_claims_are_rejected(service):
    client, manager, _ = service
    headers = owner_headers(client)
    cid = start(client, headers)['call_id']
    manager.browser_tests.claim(cid, headers['X-CSRF-Token'])
    with pytest.raises(ValueError): manager.browser_tests.claim(cid, headers['X-CSRF-Token'])
    asyncio.run(manager.hangup_call(cid))
    cid = start(client, headers)['call_id']
    with manager.db.connect() as db:
        db.execute('UPDATE browser_test_calls SET created_at=0 WHERE call_id=?', (cid,))
    with pytest.raises(ValueError): manager.browser_tests.claim(cid, headers['X-CSRF-Token'])


def test_private_origin_uses_its_separate_browser_session(portal):
    _, private, manager, _ = portal
    sign_in(private)
    token = private.cookies.get('phone-private-owner')
    session = private.app.state.web_auth.session(token, origin=PRIVATE, allow_legacy=False)
    result = start(private, {'Origin': PRIVATE, 'X-CSRF-Token': session['csrf']})
    assert result['browser_test']['scenario'] == 'direct'
    assert not manager.gateway.calls


def test_closed_office_uses_policy_without_starting_voice(service, monkeypatch):
    from app.calling.hours import HoursSettings
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    manager.hours.set(HoursSettings(enabled=True, office_hours=[], outside_hours='closed'))
    connection = FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    headers = owner_headers(client)
    cid = start(client, headers)['call_id']
    with client.websocket_connect('/live/browser-tests/' + cid + '/audio', headers=headers) as ws:
        ws.send_json({'event':'start', 'csrf':headers['X-CSRF-Token']})
        event = ws.receive_json()
        assert event['event'] == 'test_announcement' and event['ending']
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert manager.db.get(cid)['status'] == 'completed' and connection.config is None


def test_browser_creation_needs_voice_but_no_twilio_configuration(service, monkeypatch):
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    manager.settings.twilio_account_sid = ''
    manager.settings.twilio_auth_token = ''
    manager.settings.twilio_phone_number = ''
    cid = start(client)['call_id']
    assert manager.result(cid)['twilio_call_sid'] is None
    assert asyncio.run(manager.hangup_call(cid))['status'] == 'completed'


def test_revoked_browser_session_stops_audio(service, monkeypatch):
    client, manager, gateway = service
    no_phone(monkeypatch, gateway)
    connection = FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
    headers = owner_headers(client)
    cid = start(client, headers)['call_id']
    with client.websocket_connect('/live/browser-tests/' + cid + '/audio', headers=headers) as ws:
        ws.send_json({'event':'start', 'csrf':headers['X-CSRF-Token']})
        assert ws.receive_json()['event'] == 'ready'
        with manager.db.connect() as db: db.execute('DELETE FROM web_sessions')
        ws.send_json({'event':'media', 'media':{'payload':base64.b64encode(b'\xff'*160).decode(), 'timestamp':0}})
        while True:
            try: ws.receive_json()
            except WebSocketDisconnect: break
    assert manager.db.get(cid)['status'] == 'failed' and cid not in manager.bridges
