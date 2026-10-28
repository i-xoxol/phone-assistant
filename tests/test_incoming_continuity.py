"""Synthetic regression: a caller's message and a live context refresh are one call."""
import asyncio

import pytest

from app.calling.inbound import INCOMING_CALL_CONTINUITY, INCOMING_CONTINUITY_POLICY
from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from app.storage.models import CallUpdateRequest
from tests.test_call_updates import UpdateConnection
from tests.test_calls import service
from tests.test_live import Event


def incoming(manager, *, previous=False):
    cid = manager.db.create_inbound('CA_CONTINUITY', '+12025550101', 5, 10, 'concierge')
    manager.transition(cid, 'in_progress')
    if previous:
        manager.db.transcript(cid, {'type':'session.output_transcript.delta', 'event_id':'opening',
            'start_ms':0, 'end_ms':1000, 'delta':'Hello, how can I help?'})
    connection = UpdateConnection()
    bridge = MediaBridge(None, connection, manager, cid, 'MZ_CONTINUITY')
    manager.attach_bridge(cid, bridge)
    return cid, connection, bridge


@pytest.mark.parametrize('mode', ['messages', 'concierge'])
@pytest.mark.parametrize('continued', [False, True])
def test_voice_and_backend_preserve_call_progress(service, mode, continued):
    _, manager, _ = service
    cid = manager.db.create_inbound('CA_PROMPT_CONTINUITY', '+12025550101', 5, 10, mode)
    record = manager.voice_record(cid)
    if continued:record['transcript'] = [{'speaker':'callee','text':'Please pass on my message.'}]
    config = session_config(manager.settings, record)
    assert INCOMING_CONTINUITY_POLICY in config['instructions']
    assert INCOMING_CONTINUITY_POLICY in config['delegation']['responses']['instructions']
    assert 'one-time startup instruction' in config['input'][0]['content'][0]['text']
    assert 'save_message' in config['instructions'] and 'confirmation' in INCOMING_CONTINUITY_POLICY


def test_long_message_context_refresh_and_hello_do_not_requeue_opening(service, monkeypatch):
    _, manager, gateway = service
    cid, connection, bridge = incoming(manager)
    monkeypatch.setattr('app.calling.live_session.GREETING_NUDGE_SECONDS', 0)

    async def exercise():
        receiver = asyncio.create_task(bridge.receive_live())
        try:
            await bridge.send_pending_updates()
            assert not connection.updates  # Keep the immediate startup greeting.
            for speaker, text, start in [
                ('assistant', 'Hello, how can I help?', 2000),
                ('callee', 'This is Casey from downstairs.', 11000),
                ('assistant', 'What would you like me to pass along?', 25000),
                ('callee', 'Please ask the owner to switch off the noisy fan.', 35000),
            ]:
                await connection.events.put(Event(type='session.'+('output' if speaker=='assistant' else 'input')+'_transcript.delta',
                    event_id=str(start), delta=text, start_ms=start, end_ms=start+1000))
            async with asyncio.timeout(1):
                while not (bridge.assistant_spoke and bridge.caller_spoke):await asyncio.sleep(0)
            # Same automatic profile/availability notification as in a live conversation.
            await manager.context_changed()
            assert len(connection.updates) == 1
            update = connection.updates[0][1]
            assert update['delegation_id'] is None
            assert update['content'].startswith(INCOMING_CALL_CONTINUITY)
            assert 'delegate to get_call_context' in update['content']
            bridge.update_event({'type':'session.instructions.appended','client_event_id':update['event_id']})
            await connection.events.put(Event(type='session.input_transcript.delta', event_id='late_message',
                delta='I need some rest. Can you pass that on? Hello? Are you there?', start_ms=64000,end_ms=70000))
            async with asyncio.timeout(1):
                while len(manager.db.get(cid)['transcript']) < 5:await asyncio.sleep(0)
            await bridge.send_pending_updates()
            await bridge.nudge_greeting()
            assert len(connection.updates) == 1  # No repeat opening or extra tool action.
            result = manager.db.get(cid)
            assert 'noisy fan' in ''.join(t['text'] for t in result['transcript'])
            assert result['call_updates'][0]['status'] == 'acknowledged'
            assert not gateway.calls and result['status'] == 'in_progress'
        finally:
            await connection.events.put(Event(type='session.closed'))
            await receiver
    asyncio.run(exercise())


@pytest.mark.parametrize('mode,channel', [('context','thinking'),('instructions','instructions'),('say','commentary')])
def test_all_midcall_update_channels_keep_original_payload_and_progress(service, mode, channel):
    _, manager, _ = service
    cid, connection, bridge = incoming(manager)
    bridge.assistant_spoke = True
    async def exercise():
        row = await manager.send_call_update(cid, CallUpdateRequest(mode=mode,content='Use the current verified office hours.'))
        sent_channel, command = connection.updates[0]
        assert sent_channel == channel and command['content'].startswith(INCOMING_CALL_CONTINUITY)
        assert command['content'].endswith(row['content']) and 'no expanded authorization' in command['content']
        assert len(command['content']) < 1200
    asyncio.run(exercise())


def test_resumed_call_gets_continuity_once_without_startup_nudge(service, monkeypatch):
    _, manager, _ = service
    cid, connection, bridge = incoming(manager, previous=True)
    monkeypatch.setattr('app.calling.live_session.GREETING_NUDGE_SECONDS', 0)
    async def exercise():
        update = await manager.send_call_update(cid, CallUpdateRequest(content='Keep listening to this caller.'))
        await asyncio.gather(bridge.send_pending_updates(), bridge.send_pending_updates())
        await bridge.nudge_greeting()
        assert len(connection.updates) == 1
        assert connection.updates[0][1]['event_id'] == 'call_update_'+update['update_id']
        assert connection.updates[0][1]['content'].startswith(INCOMING_CALL_CONTINUITY)
        assert manager.db.get(cid)['status'] == 'in_progress'
    asyncio.run(exercise())
