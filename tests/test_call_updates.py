import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from app.mcp.server import create_mcp
from app.storage.database import Database
from app.storage.models import CallUpdateRequest
from tests.test_calls import create, service
from tests.test_live import Event, FakeConnection


class UpdateConnection(FakeConnection):
    def __init__(self):
        super().__init__()
        self.updates = []
        for channel in ('thinking', 'commentary', 'instructions'):
            async def append(channel=channel, **kwargs):
                self.updates.append((channel, kwargs))
            setattr(self.session, channel, SimpleNamespace(append=append))


def test_rest_update_validation_auth_persistence_and_terminal_idempotence(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    path = f'/calls/{cid}/updates'
    request = {'content':'Tuesday 2–4 PM Eastern is also available.', 'update_id':str(uuid4())}
    assert client.post(path, json=request, headers={'Authorization':'Bearer wrong'}).status_code == 401
    for bad in ({'content':' '}, {'content':'x'*401}, {'content':'я'*201}, {'content':'Available', 'mode':'unknown'}):
        assert client.post(path, json=bad).status_code == 422
    result = client.post(path, json=request).json()
    assert result['status'] == 'queued'
    assert client.post(path, json=request).json()['update_id'] == result['update_id']
    saved = Database(manager.db.path).get(cid)['call_updates']
    assert len(saved) == 1 and saved[0]['content'] == request['content']
    manager.transition(cid, 'no_answer')
    assert client.post(path, json=request).json()['status'] == 'rejected'
    assert client.post(path, json={'content':'A new update'}).status_code == 409
    assert client.post(path, json={**request, 'content':'Different information'}).status_code == 409
    assert client.post(f'/calls/{uuid4()}/updates', json=request).status_code == 404


@pytest.mark.parametrize('mode,channel', [('context','thinking'), ('say','commentary'), ('instructions','instructions')])
def test_live_update_routes_and_correlated_acknowledgments(service, mode, channel):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    manager.transition(cid, 'in_progress')
    async def exercise():
        connection = UpdateConnection()
        bridge = MediaBridge(None, connection, manager, cid, 'MZ_UPDATES')
        manager.attach_bridge(cid, bridge)
        request = CallUpdateRequest(content='Also ask about parking.', mode=mode, update_id=uuid4())
        result = await manager.send_call_update(cid, request)
        assert result['status'] == 'sent' and result['acknowledged_at'] is None
        sent_channel, command = connection.updates[0]
        assert sent_channel == channel and command['delegation_id'] is None
        assert command['content'].endswith(request.content) and 'no expanded authorization' in command['content']
        assert len(command['content'].encode()) < 500
        assert not bridge.update_event({'type':f'session.{channel}.appended', 'client_event_id':'unrelated'})
        bridge.update_event({'type':f'session.{channel}.appended', 'client_event_id':command['event_id'],
                             'start_ms':100, 'end_ms':300})
        result = await manager.send_call_update(cid, request)
        assert result['status'] == 'acknowledged' and result['acknowledged_at']
        assert result['end_ms'] == 300 and len(connection.updates) == 1
    asyncio.run(exercise())


def test_rejected_append_does_not_end_voice_receiver(service):
    client, manager, gateway = service
    cid = create(client).json()['call_id']
    manager.transition(cid, 'in_progress')
    async def exercise():
        connection = UpdateConnection()
        bridge = MediaBridge(None, connection, manager, cid, 'MZ_REJECT')
        manager.attach_bridge(cid, bridge)
        row = await manager.send_call_update(cid, CallUpdateRequest(content='A brief update.'))
        await connection.events.put(Event(type='error', error={'client_event_id':'call_update_'+row['update_id'],
                                                              'code':'invalid_request', 'message':'private payload'}))
        await connection.events.put(Event(type='session.closed'))
        await bridge.receive_live()
        result = manager.db.call_update(cid, row['update_id'])
        assert result['status'] == 'rejected' and result['error_code'] == 'provider_rejected'
        assert 'private payload' not in json.dumps(result)
        assert gateway.status == 'queued' and manager.status(cid)['status'] == 'in_progress'
    asyncio.run(exercise())


def test_send_failure_is_unconfirmed_and_not_retried(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    manager.transition(cid, 'in_progress')
    async def exercise():
        connection = UpdateConnection()
        bridge = MediaBridge(None, connection, manager, cid, 'MZ_FAIL')
        manager.attach_bridge(cid, bridge)
        attempts = []
        async def fail(**kwargs):
            attempts.append(kwargs)
            raise RuntimeError('do-not-store-provider-secret')
        connection.session.thinking.append = fail
        request = CallUpdateRequest(content='Available Thursday.', update_id=uuid4())
        row = await manager.send_call_update(cid, request)
        assert row['status'] == 'unconfirmed'
        assert (await manager.send_call_update(cid, request))['status'] == 'unconfirmed'
        assert len(attempts) == 1 and 'provider-secret' not in json.dumps(row)
    asyncio.run(exercise())


def test_keypad_handoff_restores_sent_context_and_sends_queued_updates_once(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    manager.transition(cid, 'in_progress')
    async def exercise():
        original = UpdateConnection()
        old = MediaBridge(None, original, manager, cid, 'MZ_OLD')
        manager.attach_bridge(cid, old)
        first = await manager.send_call_update(cid, CallUpdateRequest(content='Available Tuesday 2 PM.'))
        old.handoff = True
        second = await manager.send_call_update(cid, CallUpdateRequest(content='Correction: Tuesday 3 PM, not 2 PM.'))
        assert second['status'] == 'queued' and len(original.updates) == 1
        record = manager.db.get(cid)
        config = session_config(manager.settings, record)
        assert first['content'] in config['instructions'] and second['content'] not in config['instructions']
        assert 'must NOT authorize payments' in config['instructions']
        manager.db.restore_updates(cid, record['call_updates'])
        replacement = UpdateConnection()
        new = MediaBridge(None, replacement, manager, cid, 'MZ_NEW')
        manager.attach_bridge(cid, new)
        await asyncio.gather(new.send_pending_updates(), new.send_pending_updates())
        assert len(replacement.updates) == 1 and replacement.updates[0][1]['content'].endswith(second['content'])
        assert manager.db.call_update(cid, first['update_id'])['restored_at']
        # An acknowledgment from another voice session cannot acknowledge this update.
        assert not old.update_event({'type':'session.thinking.appended', 'client_event_id':'call_update_'+second['update_id']})
        new.end_requested.set()
        with pytest.raises(ValueError, match='ending'):
            await manager.send_call_update(cid, CallUpdateRequest(content='Too late.'))
    asyncio.run(exercise())


def test_mcp_update_is_queued_and_retrievable(service):
    _, manager, _ = service
    manager.settings.oauth_database_path = manager.db.path.parent/'oauth.db'
    server, _ = create_mcp(manager)
    async def exercise():
        placed = await server.call_tool('place_call', {'phone_number':'+12025550101', 'objective':'Discuss appointment times'})
        cid = json.loads(placed.content[0].text)['call_id']
        args = {'call_id':cid, 'content':'Thursday afternoon is available.', 'mode':'context', 'update_id':str(uuid4())}
        first = json.loads((await server.call_tool('send_call_update', args)).content[0].text)
        assert first['status'] == 'queued'
        assert not (await server.call_tool('send_call_update', args)).is_error
        result = json.loads((await server.call_tool('get_call_result', {'call_id':cid})).content[0].text)
        assert len(result['call_updates']) == 1 and result['call_updates_supported']
    asyncio.run(exercise())


def test_cancellation_during_send_preserves_uncertainty(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    manager.transition(cid, 'in_progress')
    async def exercise():
        connection = UpdateConnection()
        bridge = MediaBridge(None, connection, manager, cid, 'MZ_CANCEL')
        manager.attach_bridge(cid, bridge)
        async def interrupted(**kwargs):
            raise asyncio.CancelledError()
        connection.session.thinking.append = interrupted
        request = CallUpdateRequest(content='Available Friday.', update_id=uuid4())
        with pytest.raises(asyncio.CancelledError):
            await manager.send_call_update(cid, request)
        row = manager.db.call_update(cid, str(request.update_id))
        assert row['status'] == 'unconfirmed' and row['error_code'] == 'send_interrupted'
        assert (await manager.send_call_update(cid, request))['status'] == 'unconfirmed'
    asyncio.run(exercise())
