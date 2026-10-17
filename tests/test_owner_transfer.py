import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from twilio.request_validator import RequestValidator

from app.calling.context import utcnow
from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from app.calling.voices import VoiceChoice,VoicePreferences
from app.storage.models import SupervisorReply
from tests.test_calls import service
from tests.test_inbound import incoming,SID
from tests.test_live import FakeConnection,FakeClient,stream_headers

CHILD='CA'+'3'*32
OWNER='+12025550109'


def setup(client,m,g,caller='+12025550101'):
    m.settings.inbound_mode='concierge'
    m.settings.owner_transfer_enabled=True
    m.settings.owner_transfer_number=OWNER
    incoming(client,m,From=caller)
    cid=m.db.by_twilio_sid(SID)['call_id']
    m.transition(cid,'in_progress')
    bridge=MediaBridge(None,FakeConnection(),m,cid,'MZ_FIRST')
    m.attach_bridge(cid,bridge)
    g.redirects=[]
    g.status='in-progress'
    async def redirect(sid,xml):
        assert bridge.handoff and bridge.playback_sealed
        g.redirects.append((sid,xml))
    g.redirect_call=redirect
    return cid,bridge


def callback(client,tid,action,**changes):
    fields={'AccountSid':'AC123','CallSid':CHILD,'ParentCallSid':SID,'To':OWNER,**changes}
    path='/twilio/owner-transfer/'+tid+'/'+action
    sig=RequestValidator('twilio-test').compute_signature('https://example.test'+path,fields)
    return client.post(path,data=fields,headers={'X-Twilio-Signature':sig})


def test_voice_preferences_persist_owner_only_and_pin_existing_calls(service):
    client,m,g=service
    assert client.put('/voice',json={'voice':'meridian'},headers={'Authorization':''}).status_code==401
    assert client.put('/voice',json={'voice':'not-a-voice'}).status_code==422
    assert client.put('/voice',json={'voice':'meridian'}).json()['applies_to']=='new_calls'
    cid,bridge=setup(client,m,g)
    first=m.voice_record(cid)
    assert session_config(m.settings,first)['audio']['output']['voice']=='meridian'
    assert VoicePreferences(m.db,m.settings).get()['voice']=='meridian'
    client.put('/voice',json={'voice':'gleam'})
    assert m.voice_record(cid)['voice_name']=='meridian'
    assert OWNER not in json.dumps(first)
    async def voice_cannot_change_default():
        with pytest.raises(ValueError):
            await bridge.tools.operation({'name':'set_voice','call_id':'bad','arguments':'{"voice":"gleam"}'})
    asyncio.run(voice_cannot_change_default())


def test_screened_handoff_accepts_once_without_leaking_destination(service):
    client,m,g=service
    cid,bridge=setup(client,m,g)
    result=asyncio.run(m.transfer.request(cid,'This question needs the owner personally.'))
    tid=result['transfer_id']
    xml=g.redirects[0][1]
    assert '<Dial' in xml and 'answerOnBridge="true"' in xml and 'record="do-not-record"' in xml
    assert OWNER in xml and '/screen' in xml and '/finished' in xml
    assert OWNER not in json.dumps(result) and OWNER not in json.dumps(m.voice_record(cid))
    assert callback(client,tid,'screen').status_code==200
    assert '<Gather' in callback(client,tid,'screen').text
    accepted=callback(client,tid,'accept',Digits='1')
    assert '<Hangup' not in accepted.text and '<Dial' not in accepted.text
    assert m.transfer.for_call(cid)['accepted']==1
    # A duplicate decision must not turn acceptance into a new attempt or a decline.
    assert '<Hangup' not in callback(client,tid,'accept',Digits='2').text
    asyncio.run(m.transfer.request(cid,'Duplicate request'))
    assert len(g.redirects)==1
    finished=callback(client,tid,'finished',CallSid=SID,DialCallSid=CHILD,DialCallStatus='completed',DialBridged='true')
    assert '<Hangup' in finished.text and '<Connect' not in finished.text
    assert m.transfer.for_call(cid)['status']=='completed'


@pytest.mark.parametrize('outcome',['no-answer','busy','failed','canceled','declined'])
def test_unavailable_owner_returns_parent_to_assistant_without_redial(service,outcome):
    client,m,g=service
    cid,_=setup(client,m,g)
    tid=asyncio.run(m.transfer.request(cid,'Please help with a complex question.'))['transfer_id']
    if outcome=='declined':
        assert '<Hangup' in callback(client,tid,'accept',Digits='2').text
    done=callback(client,tid,'finished',CallSid=SID,DialCallSid=CHILD,
        DialCallStatus='completed' if outcome=='declined' else outcome,DialBridged='false')
    assert done.status_code==200 and '<Connect><Stream' in done.text and OWNER not in done.text
    assert m.transfer.for_call(cid)['status']=='returned_to_assistant'
    assert '<Hangup' in callback(client,tid,'accept',Digits='1').text
    asyncio.run(m.transfer.request(cid,'Try again'))
    assert len(g.redirects)==1


def test_no_answer_reconnect_retains_voice_and_history(service,monkeypatch):
    client,m,g=service
    cid,old=setup(client,m,g)
    m.voices.set(VoiceChoice(voice='meridian'))
    m.voice_record(cid)
    m.db.transcript(cid,{'type':'session.input_transcript.delta','event_id':'earlier','delta':'Please try the owner.',
        'start_ms':1000,'end_ms':1500})
    tid=asyncio.run(m.transfer.request(cid,'Caller needs owner help.'))['transfer_id']
    callback(client,tid,'finished',CallSid=SID,DialCallSid=CHILD,DialCallStatus='no-answer',DialBridged='false')
    m.voices.set(VoiceChoice(voice='gleam'))
    connection=FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(connection))
    path='/twilio/media/'+cid
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':SID,'streamSid':'MZ_RESUMED',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        ws.send_json({'event':'media','streamSid':'MZ_RESUMED','media':{'track':'inbound','payload':'////'}})
        assert ws.receive_json()['event']=='media'
        mark=ws.receive_json();ws.send_json({'event':'mark','streamSid':'MZ_RESUMED','mark':mark['mark']})
        ws.send_json({'event':'stop','streamSid':'MZ_RESUMED'})
        assert ws.receive_json()['event']=='clear'
    assert connection.config['audio']['output']['voice']=='meridian'
    opening=connection.config['input'][0]['content'][0]['text']
    assert 'without a new introduction' in opening and 'could not be connected' in opening
    assert 'Please try the owner.' in connection.config['instructions']
    assert m.db.get(cid)['error_message'] is None and len(g.redirects)==1


@pytest.mark.parametrize('change',[{'AccountSid':'OTHER'},{'To':'+12025550108'},
    {'ParentCallSid':'CA'+'4'*32}])
def test_transfer_callbacks_are_signed_and_bound(service,change):
    client,m,g=service
    cid,_=setup(client,m,g)
    tid=asyncio.run(m.transfer.request(cid,'Complex case.'))['transfer_id']
    assert client.post('/twilio/owner-transfer/'+tid+'/screen',data={}).status_code==403
    assert callback(client,tid,'screen',**change).status_code==403
    assert m.transfer.for_call(cid)['accepted']==0


def test_ended_call_self_dial_arbitrary_destination_and_short_budget_blocked(service):
    client,m,g=service
    cid,_=setup(client,m,g,caller=OWNER)
    with pytest.raises(ValueError,match='same phone'):
        asyncio.run(m.transfer.request(cid,'Try the owner'))
    m.settings.owner_transfer_number='+12025550108'
    assert client.post('/calls/'+cid+'/reach-owner',json={'reason':'Try','phone_number':OWNER}).status_code==422
    with m.db.connect() as conn:
        conn.execute('UPDATE calls SET started_at=? WHERE call_id=?',((utcnow()-timedelta(minutes=5)).isoformat(),cid))
    with pytest.raises(ValueError,match='Too little'):
        asyncio.run(m.transfer.request(cid,'Try the owner'))
    assert not g.redirects


def test_provider_ambiguity_does_not_retry_and_stops_paid_parent(service):
    client,m,g=service
    cid,_=setup(client,m,g)
    async def fail(*_): raise TimeoutError('secret provider data')
    g.redirect_call=fail
    with pytest.raises(ValueError,match='unconfirmed'):
        asyncio.run(m.transfer.request(cid,'Complex case'))
    assert m.db.get(cid)['status']=='completed'
    assert m.transfer.for_call(cid)['status']=='unconfirmed'
    assert 'secret' not in json.dumps(m.result(cid))


def test_owner_attempt_expires_supervision_and_blocks_competing_actions(service):
    client,m,g=service
    cid,_=setup(client,m,g)
    req=m.db.create_supervisor_request(cid,'Please book an appointment.',(utcnow()+timedelta(seconds=60)).isoformat())
    asyncio.run(m.transfer.request(cid,'Owner must handle this.'))
    assert m.db.supervisor_request(req['request_id'])['status']=='expired'
    with pytest.raises(ValueError,match='Owner contact'):
        asyncio.run(m.respond_to_supervisor(req['request_id'],SupervisorReply(response='A test answer.')))
    assert not m.db.get(cid)['call_updates']


def test_forwarding_loop_from_twilio_number_is_rejected(service):
    client,m,g=service
    assert '<Reject' in incoming(client,m,From=m.settings.twilio_phone_number).text
    assert not m.db.recent_calls()


def test_mobile_voice_settings_require_pairing_and_csrf(service):
    client,m,g=service
    assert client.put('/live/voice',json={'voice':'meridian'}).status_code==401
    client.base_url='https://example.test'
    token=client.app.state.web_auth.create_code()
    client.post('/live/login',data={'code':token},headers={'Origin':'https://example.test'})
    assert client.put('/live/voice',json={'voice':'meridian'}).status_code==403
    # Obtain the CSRF token through the same owner-rendered page, never auth-cache inspection.
    import re
    csrf=re.search('name="csrf-token" content="([^"]+)"',client.get('/live').text)[1]
    result=client.put('/live/voice',json={'voice':'meridian'},headers={'Origin':'https://example.test','X-CSRF-Token':csrf})
    assert result.status_code==200 and result.json()['voice']=='meridian'


def test_screen_omitted_parent_fetches_and_verifies_provider_binding(service):
    client,m,g=service
    cid,_=setup(client,m,g)
    tid=asyncio.run(m.transfer.request(cid,'Complex case.'))['transfer_id']
    async def verified(sid):
        assert sid==CHILD
        return SimpleNamespace(parent_call_sid=SID,to=OWNER)
    g.fetch=verified
    assert callback(client,tid,'screen',ParentCallSid='').status_code==200
    assert callback(client,tid,'screen',CallSid='CA'+'5'*32).status_code==403
    async def wrong(sid): return SimpleNamespace(parent_call_sid='OTHER',to=OWNER)
    g.fetch=wrong
    assert callback(client,tid,'accept',ParentCallSid='',Digits='1').status_code==403
    assert m.transfer.for_call(cid)['accepted']==0


def test_duplicate_media_start_cannot_replace_or_hangup_active_call(service,monkeypatch):
    client,m,g=service
    cid,original=setup(client,m,g)
    def no_live(_): raise AssertionError('Duplicate must be rejected before Live')
    monkeypatch.setattr('app.calling.live_session.live_client',no_live)
    path='/twilio/media/'+cid
    from starlette.websockets import WebSocketDisconnect
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':SID,'streamSid':'MZ_DUP',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert m.bridges[cid] is original and g.status=='in-progress'
    assert m.db.get(cid)['status']=='in_progress' and m.db.get(cid)['error_message'] is None
