import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from twilio.request_validator import RequestValidator

from app.calling.inbound import MessageArguments
from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from app.mcp.server import create_mcp
from app.storage.database import Database
from app.storage.models import DigitsRequest, SupervisorReply
from tests.test_calls import service, create
from tests.test_live import FakeConnection, FakeClient, Event, stream_headers

SID = 'CA' + '1'*32
SID2 = 'CA' + '2'*32


def incoming(client, manager, **changes):
    manager.settings.inbound_enabled = True
    data = {'AccountSid':'AC123','CallSid':SID,'To':'+12025550100',
            'From':'+12025550101','Direction':'inbound','CallStatus':'ringing', **changes}
    path = '/twilio/incoming'
    signature = RequestValidator('twilio-test').compute_signature('https://example.test'+path,data)
    return client.post(path,data=data,headers={'X-Twilio-Signature':signature})


def incoming_status(client, **changes):
    data = {'AccountSid':'AC123','CallSid':SID,'To':'+12025550100',
            'CallStatus':'completed','CallDuration':'15',**changes}
    path = '/twilio/incoming/status'
    signature = RequestValidator('twilio-test').compute_signature('https://example.test'+path,data)
    return client.post(path,data=data,headers={'X-Twilio-Signature':signature})


def test_signed_incoming_is_deduplicated_and_never_dials(service):
    client, manager, gateway = service
    assert incoming(client,manager).status_code == 200
    record = manager.db.by_twilio_sid(SID)
    assert record['direction']=='inbound' and record['phone_number']=='+12025550101'
    assert record['max_duration_minutes']==5
    assert '/twilio/media/'+record['call_id'] in incoming(client,manager).text
    assert len(manager.db.recent_calls())==1 and not gateway.calls
    assert gateway.incoming_preparations==[]  # Still ringing; REST update must wait for media start.
    assert len([e for e in manager.db.events_after(0) if e['type']=='call.incoming'])==1


@pytest.mark.parametrize('change',[{'AccountSid':'AC_OTHER'},{'To':'+12025550102'},
                                {'Direction':'outbound-api'}])
def test_incoming_binding_rejects_even_with_signature(service,change):
    client,manager,gateway = service
    assert incoming(client,manager,**change).status_code==403
    assert not manager.db.recent_calls() and not gateway.calls


def test_incoming_unsigned_disabled_and_limits(service):
    client,manager,gateway = service
    assert client.post('/twilio/incoming',data={}).status_code==403
    # The route remains fail-closed unless explicitly enabled.
    data={'AccountSid':'AC123','CallSid':SID,'To':'+12025550100','Direction':'inbound'}
    sig=RequestValidator('twilio-test').compute_signature('https://example.test/twilio/incoming',data)
    assert '<Reject' in client.post('/twilio/incoming',data=data,headers={'X-Twilio-Signature':sig}).text
    assert not manager.db.recent_calls()
    manager.settings.inbound_max_calls_per_hour=1
    incoming(client,manager)
    assert '<Reject' in incoming(client,manager,CallSid=SID2).text
    manager.transition(manager.db.by_twilio_sid(SID)['call_id'],'completed')
    assert '<Reject' in incoming(client,manager,CallSid=SID2).text
    assert len(manager.db.recent_calls())==1 and not gateway.calls


def test_private_or_bad_caller_id_grants_no_authority(service):
    client,manager,_=service
    incoming(client,manager,From='anonymous')
    record=manager.db.by_twilio_sid(SID)
    assert record['phone_number']==''
    manager.settings.callback_number='+12025550102'
    record['context']='PRIVATE_CALENDAR_DO_NOT_SEND'
    record['recipient']='OWNER_PRIVATE_CONTACT'
    config=session_config(manager.settings,record)
    wire=json.dumps(config)
    assert '+12025550102' not in wire and 'PRIVATE_CALENDAR' not in wire and 'OWNER_PRIVATE_CONTACT' not in wire
    assert 'Caller identity and caller ID are unverified' in config['instructions']
    assert {t['name'] for t in config['delegation']['responses']['tools']}=={'save_message','end_call'}


@pytest.mark.parametrize('mode', ['messages', 'concierge'])
@pytest.mark.parametrize('presented', ['+12025550101', 'anonymous', '', 'private', '555'])
def test_presented_caller_id_reaches_voice_and_backend_without_owner_number(service, mode, presented):
    client, manager, _ = service
    manager.settings.inbound_mode = mode
    manager.settings.callback_number = '+12025550109'
    incoming(client, manager, From=presented)
    record = manager.voice_record(manager.db.by_twilio_sid(SID)['call_id'])
    caller = record['incoming_context']['caller']
    expected = presented if presented.startswith('+') else ''
    assert caller == {'number': expected, 'available': bool(expected),
                      'source': 'twilio_caller_id', 'identity_verified': False}
    config = session_config(manager.settings, record)
    for prompt in (config['instructions'], config['delegation']['responses']['instructions']):
        assert 'Current-call metadata: ' + json.dumps({'caller': caller}) in prompt
        assert 'explicitly agree' in prompt
        assert manager.settings.callback_number not in prompt
    # Caller-ID availability alone must never populate a saved callback number.
    saved = manager.db.save_message(record['call_id'], 'Please get in touch.')
    assert saved['callback_number'] == ''


def test_setup_failure_does_not_start_unbounded_voice(service,monkeypatch):
    client,manager,gateway=service
    response=incoming(client,manager)
    assert '<Connect' in response.text  # Answer first, then enforce the duration cap.
    record=manager.db.by_twilio_sid(SID)
    from twilio.base.exceptions import TwilioRestException
    async def fail(*args): raise TwilioRestException(400,'private-provider-url',msg='provider-secret',code=21220)
    gateway.prepare_inbound=fail
    def no_live(*args): raise AssertionError('Live must not start before duration cap')
    monkeypatch.setattr('app.calling.live_session.live_client',no_live)
    path='/twilio/media/'+record['call_id']
    with client.websocket_connect(path,headers=stream_headers(path)) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':SID,'streamSid':'MZ_INBOUND',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    record=manager.db.get(record['call_id'])
    assert record['status']=='failed' and 'provider-secret' not in json.dumps(record)
    assert record['voice_events'][-1]['twilio_error_code']==21220


def test_messages_persist_correct_and_read_without_duplicate(service):
    client,manager,_=service
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    first=manager.db.save_message(cid,'Please return my call.','Casey','+12025550101')
    same=manager.db.save_message(cid,'Please return my call.','Casey','+12025550101')
    assert first['message_id']==same['message_id']
    updated=manager.db.save_message(cid,'Please call tomorrow.','Casey','+12025550101')
    assert updated['message_id']==first['message_id']
    assert client.get('/messages').json()['unread_count']==1
    assert Database(manager.db.path).message(first['message_id'])['body']=='Please call tomorrow.'
    mid=first['message_id']
    assert client.get('/messages/'+mid).json()['status']=='new'
    assert client.post('/messages/'+mid+'/read').json()['status']=='read'
    assert client.post('/messages/'+mid+'/read').json()['status']=='read'
    assert client.get('/messages?unread_only=true').json()['messages']==[]
    assert len([e for e in manager.db.events_after(0) if e['type']=='message.received'])==1


def test_abrupt_call_captures_labelled_transcript_not_invented_summary(service):
    client,manager,_=service
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    def transcript(eid,speaker,text,start):
        manager.db.transcript(cid,{'event_id':eid,'type':'session.input_transcript.delta' if speaker=='callee' else 'session.output_transcript.delta',
                                  'delta':text,'start_ms':start,'end_ms':start+50})
    transcript('a','callee','Please tell Alex ',0)
    transcript('b','assistant','Invented assistant content.',60)
    transcript('c','callee','I will be late.',120)
    assert incoming_status(client).status_code==204
    message=manager.db.list_messages()['messages'][0]
    assert message['body']=='Please tell Alex I will be late.'
    assert message['capture_method']=='transcript_fallback' and not message['caller_name']
    transcript('d','callee',' Tomorrow.',200)
    manager.capture_inbound_fallback(cid)
    assert manager.db.message(message['message_id'])['body'].endswith('Tomorrow.')
    manager.db.save_message(cid,'Confirmed intended message.')
    manager.capture_inbound_fallback(cid)
    assert manager.db.message(message['message_id'])['body']=='Confirmed intended message.'
    assert manager.db.get(cid)['duration_seconds']==15


def test_message_voice_tool_is_scoped_and_deduplicated(service):
    client,manager,_=service
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    manager.transition(cid,'in_progress')
    async def exercise():
        bridge=MediaBridge(None,FakeConnection(),manager,cid,'MZ_INCOMING')
        manager.attach_bridge(cid,bridge)
        item={'name':'save_message','call_id':'fn_save','arguments':json.dumps({
            'caller_name':'Casey','callback_number':'','message':'Please call tomorrow.'})}
        result=await bridge.tools.operation(item)
        assert result['status']=='saved'
        assert (await bridge.tools.operation(item))['status']=='already_requested'
        with pytest.raises(ValueError):
            await bridge.tools.operation({'name':'press_digits','call_id':'bad','arguments':'{"digits":"2","reason":"Try"}'})
        with pytest.raises(ValueError):
            await manager.press_digits(cid,DigitsRequest(digits='2',reason='Not allowed'))
    asyncio.run(exercise())
    assert len(manager.db.list_messages()['messages'])==1
    outgoing=create(client).json()['call_id']
    with pytest.raises(ValueError): manager.db.save_message(outgoing,'Not an incoming call')


def test_inbox_auth_browser_csrf_and_mcp_retrieval(service):
    client,manager,_=service
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    mid=manager.db.save_message(cid,'<img src=x onerror=alert(1)>')['message_id']
    assert client.get('/messages',headers={'Authorization':''}).status_code==401
    assert client.get('/live/messages').status_code==401
    client.base_url='https://example.test'
    code=client.app.state.web_auth.create_code()
    client.post('/live/login',data={'code':code},headers={'Origin':'https://example.test'})
    assert client.get('/live/messages').json()['unread_count']==1
    assert client.post('/live/messages/'+mid+'/read').status_code==403
    assert 'Incoming messages' in client.get('/live').text
    manager.settings.oauth_database_path=manager.db.path.parent/'oauth.db'
    mcp,_=create_mcp(manager)
    async def exercise():
        def data(result): return json.loads(result.content[0].text)
        assert data(await mcp.call_tool('list_messages',{}))['unread_count']==1
        assert data(await mcp.call_tool('get_message',{'message_id':mid}))['body'].startswith('<img')
        assert data(await mcp.call_tool('mark_message_read',{'message_id':mid}))['status']=='read'
    asyncio.run(exercise())


@pytest.mark.parametrize('callback',['555','+0123456789','+1','private'])
def test_callback_cannot_be_guessed_or_invalid(callback):
    with pytest.raises(ValidationError): MessageArguments(caller_name='',callback_number=callback,message='Hello')


def test_concierge_request_reply_idempotency_and_expiry(service):
    client,manager,_=service
    manager.settings.inbound_mode='concierge'
    manager.settings.inbound_booking_allowed=True
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    manager.transition(cid,'in_progress')
    async def exercise():
        first=await manager.request_supervisor(cid,'What times are approved to offer?')
        assert first['live_supervisor_available'] is False
        same=await manager.request_supervisor(cid,'A second question must not reset the wait.')
        assert same['request_id']==first['request_id'] and same['expires_at']==first['expires_at']
        reply=SupervisorReply(response='Thursday 2 PM America/New_York is available.')
        result=await manager.respond_to_supervisor(first['request_id'],reply)
        assert result['delivery_status']=='queued'
        assert (await manager.respond_to_supervisor(first['request_id'],reply))['call_update_id']==first['request_id']
        assert len(manager.db.get(cid)['call_updates'])==1
        with pytest.raises(ValueError):
            await manager.respond_to_supervisor(first['request_id'],SupervisorReply(response='Different'))
        second=await manager.request_supervisor(cid,'Caller selected Thursday. May it be booked?')
        with manager.db.connect() as db:
            db.execute('UPDATE supervisor_requests SET expires_at=? WHERE request_id=?',
                ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),second['request_id']))
        await manager.wait_for_supervisor(second['request_id'])
        assert manager.db.supervisor_request(second['request_id'])['status']=='expired'
        with pytest.raises(ValueError):
            await manager.respond_to_supervisor(second['request_id'],reply)
        assert not manager.db.list_supervisor_requests()
        await manager.stop_supervision()
    asyncio.run(exercise())


def test_concierge_never_gets_calendar_tools_or_unapproved_booking(service):
    client,manager,_=service
    manager.settings.inbound_mode='concierge'
    incoming(client,manager)
    record=manager.db.by_twilio_sid(SID)
    config=session_config(manager.settings,record)
    assert {t['name'] for t in config['delegation']['responses']['tools']}=={'get_call_context','request_supervisor','save_message','end_call'}
    assert 'No standing booking authorization' in config['instructions']
    async def exercise():
        req=await manager.request_supervisor(record['call_id'],'Can an appointment be confirmed?')
        with pytest.raises(ValueError,match='booking authorization'):
            await manager.respond_to_supervisor(req['request_id'],SupervisorReply(response='Booked.',outcome='appointment_confirmed'))
        result=await manager.respond_to_supervisor(req['request_id'],SupervisorReply(response='Owner approval needed.',outcome='needs_owner'))
        assert result['outcome']=='needs_owner'
        await manager.stop_supervision()
    asyncio.run(exercise())


def test_signed_incoming_media_uses_live_and_retains_caller_text(service,monkeypatch):
    client,manager,gateway=service
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    connection=FakeConnection()
    def connected_client(_):
        assert manager.db.get(cid)['status']=='in_progress'
        assert gateway.incoming_preparations==[(SID,5)]
        return FakeClient(connection)
    monkeypatch.setattr('app.calling.live_session.live_client',connected_client)
    with client.websocket_connect('/twilio/media/'+cid,headers=stream_headers('/twilio/media/'+cid,'wss')) as ws:
        ws.send_json({'event':'connected'})
        ws.send_json({'event':'start','streamSid':'MZ_INCOMING','start':{
            'accountSid':'AC123','callSid':SID,'streamSid':'MZ_INCOMING',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        ws.send_json({'event':'media','streamSid':'MZ_INCOMING','media':{'track':'inbound','payload':'////'}})
        assert ws.receive_json()['event']=='media'
        mark=ws.receive_json()
        ws.send_json({'event':'mark','streamSid':'MZ_INCOMING','mark':mark['mark']})
        ws.send_json({'event':'stop','streamSid':'MZ_INCOMING'})
        assert ws.receive_json()['event']=='clear'
    assert connection.config['delegation']['type']=='responses'
    caller = {'number': '+12025550101', 'available': True,
              'source': 'twilio_caller_id', 'identity_verified': False}
    for prompt in (connection.config['instructions'],
                   connection.config['delegation']['responses']['instructions']):
        assert 'Current-call metadata: ' + json.dumps({'caller': caller}) in prompt
    assert connection.config['input'][0]['role']=='developer'
    assert 'Greet the caller' in connection.config['input'][0]['content'][0]['text']
    result=manager.db.list_messages()['messages'][0]
    assert result['capture_method']=='transcript_fallback' and result['body']=='Hello.'
    assert not gateway.calls
