import asyncio
import json
from app.calling.message_notifications import message_notification_event
from app.mcp.events import definitions
from jsonschema import validate
from tests.test_calls import service
from tests.test_inbound import incoming,SID
from tests.test_recordings import enable,recording_callback,RID
from tests.test_mcp_events import events,SECRET
from app.mcp.events import Subscribe


def prepare(service,tmp_path,*,forwarded=True):
    client,m,_=service
    m.settings.inbound_missed_call_context=True
    m.settings.missed_call_notifications_enabled=True
    m.settings.owner_transfer_number='+12025550109'
    enable(m,tmp_path)
    incoming(client,m,**({'ForwardedFrom':m.settings.owner_transfer_number} if forwarded else {}))
    cid=m.db.by_twilio_sid(SID)['call_id']
    return client,m,cid


def last_event(m,kind):
    return next(e for e in reversed(m.db.events_after(0,limit=100)) if e['type']==kind)


def test_missed_message_notice_then_recording_ready_reuses_existing_subscription(service,tmp_path):
    client,m,cid=prepare(service,tmp_path)
    msg=m.db.save_message(cid,'Please call about the appointment.',caller_name='Casey')
    event=last_event(m,'message.received')
    notice=message_notification_event(m,event)
    assert notice['id']==event['id'] and notice['data']['notify_owner']
    assert notice['data']['phase']=='message_saved' and notice['data']['recording_status']=='processing'
    assert notice['data']['message_preview']==msg['body']
    recording_callback(client,cid)
    source=last_event(m,'call.recording')
    ready=message_notification_event(m,source)
    assert ready['type']=='message.received' and ready['id']==source['id']
    assert ready['data']['message_id']==msg['message_id'] and ready['data']['phase']=='recording_ready'
    assert ready['data']['recordings']==[{'recording_id':RID,'duration_seconds':12}]
    payload={**ready['data'],'call_id':cid,'url':'https://example.test/live?call_id='+cid}
    schema=next(d['payloadSchema'] for d in definitions() if d['name']=='message.received')
    validate(payload,schema)
    assert m.settings.owner_transfer_number not in json.dumps(payload)
    assert 'api.twilio.com' not in json.dumps(payload)


def test_recording_finishes_before_message_and_duplicates_have_no_extra_source_event(service,tmp_path):
    client,m,cid=prepare(service,tmp_path)
    recording_callback(client,cid)
    assert message_notification_event(m,last_event(m,'call.recording')) is None
    m.db.save_message(cid,'A later message.',capture_method='transcript_fallback')
    notice=message_notification_event(m,last_event(m,'message.received'))
    assert notice['data']['recording_status']=='ready' and notice['data']['recordings']
    head=m.db.event_head()
    recording_callback(client,cid)
    assert m.db.event_head()==head


def test_direct_messages_do_not_gain_missed_call_alerts_and_flag_can_disable(service,tmp_path):
    client,m,cid=prepare(service,tmp_path,forwarded=False)
    m.db.save_message(cid,'A direct call message.')
    original=last_event(m,'message.received')
    assert message_notification_event(m,original)==original
    recording_callback(client,cid)
    assert message_notification_event(m,last_event(m,'call.recording')) is None
    m.settings.missed_call_notifications_enabled=False
    assert message_notification_event(m,original)==original


def test_existing_message_subscription_delivers_recording_update_once(events,tmp_path):
    bridge,_,received,_=events;m=bridge.manager
    m.settings.inbound_missed_call_context=True;m.settings.missed_call_notifications_enabled=True
    m.settings.owner_transfer_number='+12025550109'
    async def exercise():
        params=Subscribe(name='message.received',arguments={},
            delivery={'mode':'webhook','url':'https://callback.example.com/events','secret':SECRET})
        sub=await bridge.subscribe(None,params)
        cid=m.db.create_inbound(SID,'+12025550101',5,10,'messages')
        m.routing.save(cid,{'ForwardedFrom':m.settings.owner_transfer_number})
        m.db.save_message(cid,'Caller asks for a callback.')
        worker=asyncio.create_task(bridge.worker(sub['id']))
        try:
            async with asyncio.timeout(3):
                while bridge.get(sub['id'])['delivered_events']<1: await asyncio.sleep(.01)
            m.db.emit(cid,'call.recording',{'state':'requested'})
            # A completed recording must actually exist in the recording store.
            from app.calling.context import utcnow
            with m.db.connect() as db:
                db.execute('INSERT INTO call_recordings VALUES (?,?,?,?,?,?,?)',
                    (RID,cid,'completed',12,2,utcnow().isoformat(),utcnow().isoformat()))
            m.db.emit(cid,'call.recording',{'state':'completed','recording_id':RID})
            async with asyncio.timeout(3):
                while bridge.get(sub['id'])['delivered_events']<2: await asyncio.sleep(.01)
            notices=[e for e,_ in received if e.get('name')=='message.received']
            assert [e['data']['phase'] for e in notices]==['message_saved','recording_ready']
            assert len({e['eventId'] for e in notices})==2
        finally:
            worker.cancel()
            await asyncio.gather(worker,return_exceptions=True)
    asyncio.run(exercise())
