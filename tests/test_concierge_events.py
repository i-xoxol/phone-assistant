import asyncio
import json
from types import SimpleNamespace

import pytest
from jsonschema import validate
from mcp.shared.exceptions import MCPError

from app.mcp.events import PhoneEvents, Subscribe, definitions
from tests.test_calls import service
from tests.test_mcp_events import events, SECRET


def subscription(name):
    return Subscribe(name=name,arguments={},delivery={'mode':'webhook',
        'url':'https://callback.example.com/events','secret':SECRET})


def test_line_wide_events_wake_supervisor_for_future_calls(events):
    bridge,_,received,_=events
    manager=bridge.manager
    manager.mcp_events=bridge
    async def exercise():
        sub=await bridge.subscribe(None,subscription('call.supervisor_requested'))
        assert bridge.ready_for('call.supervisor_requested')
        cid=manager.db.create_inbound('CA'+'3'*32,'+12025550101',5,10,'concierge')
        manager.transition(cid,'in_progress')
        request=await manager.request_supervisor(cid,'Caller asks for approved appointment times.')
        assert request['live_supervisor_available']
        worker=asyncio.create_task(bridge.worker(sub['id']))
        try:
            async with asyncio.timeout(1):
                while not any(e.get('name')=='call.supervisor_requested' for e,_ in received):
                    await asyncio.sleep(.01)
            event=next(e for e,_ in received if e.get('name')=='call.supervisor_requested')
            assert event['data']['call_id']==cid and event['data']['request_id']==request['request_id']
            definition=next(d for d in definitions() if d['name']==event['name'])
            validate(event['data'],definition['payloadSchema'])
            report=bridge.list_subscriptions(None)
            assert report['subscriptions'][0]['delivered_events']==1
            assert SECRET not in json.dumps(report)
            await bridge.unsubscribe_id(sub['id'])
            assert not bridge.ready_for('call.supervisor_requested')
        finally:
            worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)
            await manager.stop_supervision()
    asyncio.run(exercise())


@pytest.mark.parametrize('name',['call.incoming','message.received'])
def test_global_event_payloads_and_no_old_history(events,name):
    bridge,_,received,_=events
    async def exercise():
        cid=bridge.manager.db.create_inbound('CA'+'4'*32,'',5,10)
        bridge.manager.db.save_message(cid,'Old message not exported.')
        sub=await bridge.subscribe(None,subscription(name))
        assert bridge.get(sub['id'])['cursor']==bridge.manager.db.event_head()
        if name=='call.incoming':
            bridge.manager.transition(cid,'completed')
            bridge.manager.db.create_inbound('CA'+'5'*32,'',5,10)
        else:
            bridge.manager.db.emit(cid,'message.received',{'message_id':'example','capture_method':'voice_tool','status':'new'})
        event=next(e for e in bridge.manager.db.events_after(bridge.get(sub['id'])['cursor']) if e['type']==name)
        await bridge.deliver(sub['id'],event)
        value=received[-1][0]
        validate(value['data'],next(d for d in definitions() if d['name']==name)['payloadSchema'])
        assert 'Old message' not in json.dumps(received)
    asyncio.run(exercise())


def test_global_and_per_call_filters_cannot_be_confused(events):
    bridge,params,_,_=events
    for value in (params.model_copy(update={'name':'call.incoming'}),subscription('transcript.delta')):
        with pytest.raises(MCPError): bridge.identity(value,'owner')


def test_legacy_subscription_migration_preserves_grants_and_cursors(events):
    bridge,params,_,_=events
    async def exercise():
        sid=(await bridge.subscribe(None,params))['id']
        original=bridge.get(sid)
        with bridge.manager.db.connect() as db:
            schema=db.execute("SELECT sql FROM sqlite_master WHERE name='mcp_event_subscriptions'").fetchone()[0]
            legacy=schema.replace('call_id TEXT REFERENCES','call_id TEXT NOT NULL REFERENCES')
            assert legacy!=schema
            db.execute('BEGIN IMMEDIATE')
            db.execute('ALTER TABLE mcp_event_subscriptions RENAME TO test_previous_subscriptions')
            db.execute(legacy)
            db.execute('INSERT INTO mcp_event_subscriptions SELECT * FROM test_previous_subscriptions')
            db.execute('DROP TABLE test_previous_subscriptions')
        restarted=PhoneEvents(bridge.manager,bridge.provider,sender=bridge.sender)
        assert restarted.get(sid)==original
        await restarted.subscribe(None,subscription('call.supervisor_requested'))
        assert restarted.ready_for('call.supervisor_requested')
    asyncio.run(exercise())
