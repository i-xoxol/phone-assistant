import asyncio
import base64
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from uuid import uuid4

import pytest
from twilio.request_validator import RequestValidator
from app.calling.prompts import session_config
from app.calling.tools import VoiceTools
from app.calling.knowledge import ContactUpdate
from app.mcp.server import create_mcp
from tests.test_calls import service,create
from tests.test_inbound import incoming,SID

RID='RE'+'a'*32


@pytest.mark.parametrize('preamble',['', 'I will recite this exactly. '])
def test_cached_notice_rejects_spoken_preambles(service,monkeypatch,preamble):
    from scripts.prepare_recording_notices import render_notice
    _,m,_=service;sent=[]
    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        async def send(self,data): sent.append(data)
        def __aiter__(self): return self
        async def __anext__(self):
            event=next(self.events,None)
            if event is None: raise StopAsyncIteration
            return SimpleNamespace(model_dump=lambda:event)
        events=iter([{'type':'session.updated'},
            {'type':'response.output_audio.delta','delta':base64.b64encode(b'\0\0'*24000).decode()},
            {'type':'response.output_audio_transcript.done','transcript':preamble+'This call is being recorded.'},
            {'type':'response.done','response':{'status':'completed'}}])
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        realtime=SimpleNamespace(connect=lambda **_:Connection())
    monkeypatch.setattr('scripts.prepare_recording_notices.live_client',lambda _:Client())
    if preamble:
        with pytest.raises(ValueError,match='transcript does not match'):
            asyncio.run(render_notice(m.settings,'This call is being recorded.'))
    else:
        assert asyncio.run(render_notice(m.settings,'This call is being recorded.')).startswith(b'RIFF')
    response=sent[1]['response']
    assert response['input'][0]['content'][0]['text']=='This call is being recorded.'
    assert 'This call is being recorded.' not in response['instructions']


def enable(m,tmp_path):
    m.settings.call_recording_enabled=True
    m.settings.recording_notice_directory=tmp_path/'notices'
    m.settings.recording_notice_directory.mkdir()
    for language in ('en','uk'): (m.settings.recording_notice_directory/(language+'.wav')).write_bytes(b'RIFF-synthetic-notice')


def signed(client,path,data):
    sig=RequestValidator('twilio-test').compute_signature('https://example.test'+path,data)
    return client.post(path,data=data,headers={'X-Twilio-Signature':sig})


def recording_callback(client,cid,sid=SID,state='completed',**extra):
    return signed(client,'/twilio/recordings/'+cid,{'AccountSid':'AC123','CallSid':sid,
        'RecordingSid':RID,'RecordingStatus':state,'RecordingChannels':'2','RecordingDuration':'12',**extra})


def test_recording_notice_order_incoming_and_no_restart_on_duplicate(service,tmp_path):
    client,m,_=service;enable(m,tmp_path);m.settings.inbound_answer_delay_seconds=8
    response=incoming(client,m)
    root=ET.fromstring(response.text)
    assert [n.tag for n in root]==['Pause','Play','Start','Connect','Hangup']
    assert root[0].attrib['length']=='8'
    assert root.find('Start/Recording').attrib['track']=='both'
    assert root.find('Start/Recording').attrib['channels']=='dual'
    cid=m.db.by_twilio_sid(SID)['call_id']
    assert m.recordings.snapshot(cid)['policy']['notice_issued_at']
    assert '<Recording' not in incoming(client,m).text
    assert not m.recordings.snapshot(cid)['recordings']


def test_outbound_is_also_noticed_and_historical_calls_remain_unrecorded(service,tmp_path):
    client,m,_=service;cid=create(client).json()['call_id']
    assert m.recordings.snapshot(cid)['policy'] is None
    enable(m,tmp_path)
    response=signed(client,'/twilio/voice/'+cid,{'AccountSid':'AC123','CallSid':'CA123'})
    root=ET.fromstring(response.text)
    assert [n.tag for n in root][:3]==['Play','Start','Connect']
    config=session_config(m.settings,m.voice_record(cid))
    assert {t['name'] for t in config['delegation']['responses']['tools']}=={'stop_call_recording'}


def test_signed_callbacks_bind_recordings_and_never_fetch_provider_url(service,tmp_path):
    client,m,g=service;enable(m,tmp_path);incoming(client,m);cid=m.db.by_twilio_sid(SID)['call_id']
    assert client.post('/twilio/recordings/'+cid,data={}).status_code==403
    assert recording_callback(client,cid,sid='CA_OTHER').status_code==400
    assert recording_callback(client,cid,state='completed',RecordingUrl='http://127.0.0.1/secret').status_code==204
    assert recording_callback(client,cid,state='in-progress').status_code==204
    rows=m.recordings.snapshot(cid)['recordings']
    assert len(rows)==1 and rows[0]['status']=='completed' and rows[0]['duration_seconds']==12
    assert '127.0.0.1' not in json.dumps(m.result(cid))
    assert client.get('/calls/'+cid+'/recordings').status_code==200
    # Original call record is preserved, with linked recording metadata.
    from app.calling.recordings import CallRecordings
    assert CallRecordings(m).snapshot(cid)['recordings'][0]['recording_id']==RID


def test_audio_requires_owner_browser_or_api_and_recording_belongs_to_call(service,tmp_path):
    client,m,g=service;enable(m,tmp_path);incoming(client,m);cid=m.db.by_twilio_sid(SID)['call_id']
    recording_callback(client,cid)
    async def audio(rid): assert rid==RID;return b'ID3-synthetic-audio'
    g.recording_audio=audio
    url='/calls/'+cid+'/recordings/'+RID+'/audio'
    assert client.get(url,headers={'Authorization':'Bearer invalid'}).status_code==401
    response=client.get(url);assert response.content==b'ID3-synthetic-audio'
    assert response.headers['cache-control']=='no-store'
    assert client.get('/live'+url).status_code==401
    code=client.app.state.web_auth.create_code()
    client.post('/live/login',data={'code':code},headers={'Origin':'https://example.test'})
    # TestClient's default host is HTTP; provide the secure cookie explicitly.
    token=client.cookies.get('__Host-phone-owner')
    assert client.get('/live'+url,headers={'Cookie':'__Host-phone-owner='+token}).status_code==200
    other=create(client).json()['call_id']
    assert client.get('/calls/'+other+'/recordings/'+RID+'/audio').status_code==404


def test_notice_fetch_requires_twilio_signature_and_known_language(service,tmp_path):
    client,m,_=service;enable(m,tmp_path)
    path='/twilio/recording-notice/uk.wav'
    assert client.get(path).status_code==403
    sig=RequestValidator('twilio-test').compute_signature('https://example.test'+path,{})
    assert client.get(path,headers={'X-Twilio-Signature':sig}).content==b'RIFF-synthetic-notice'
    assert client.get('/twilio/recording-notice/fr.wav').status_code==404


def test_missing_notice_preserves_call_without_silent_recording(service,tmp_path):
    client,m,_=service;m.settings.call_recording_enabled=True
    m.settings.recording_notice_directory=tmp_path/'unprepared-notices'
    response=incoming(client,m)
    assert '<Connect' in response.text and '<Recording' not in response.text
    cid=m.db.by_twilio_sid(SID)['call_id']
    assert m.recordings.snapshot(cid)['last_event']['state']=='unavailable'


def test_participant_can_stop_recording_without_restarting_it(service,tmp_path):
    client,m,g=service;enable(m,tmp_path);incoming(client,m);cid=m.db.by_twilio_sid(SID)['call_id']
    m.transition(cid,'in_progress');stops=[]
    async def stop(sid): stops.append(sid)
    g.stop_recording=stop
    bridge=SimpleNamespace(manager=m,call_id=cid,closing=False)
    m.bridges[cid]=bridge
    async def run():
        result=await VoiceTools(bridge).operation({'name':'stop_call_recording','call_id':'privacy-request','arguments':'{}'})
        assert result['status']=='stopped'
        assert (await m.recordings.stop(cid))['status']=='stopped'
    asyncio.run(run())
    assert stops==[SID] and m.recordings.snapshot(cid)['policy']['stop_confirmed_at']
    assert '<Recording' not in incoming(client,m).text


def test_stop_failure_ends_call_rather_than_continue_recording(service,tmp_path):
    client,m,g=service;enable(m,tmp_path);incoming(client,m);cid=m.db.by_twilio_sid(SID)['call_id']
    m.transition(cid,'in_progress');g.status='in-progress'
    async def fail(sid): raise RuntimeError('provider-secret')
    g.stop_recording=fail
    result=asyncio.run(m.recordings.stop(cid))
    assert result['status']=='call_ended' and m.db.get(cid)['status']=='completed'
    assert 'provider-secret' not in json.dumps(m.result(cid))


def test_recording_mcp_retrieves_private_audio_and_metadata(service,tmp_path):
    client,m,g=service;enable(m,tmp_path);incoming(client,m);cid=m.db.by_twilio_sid(SID)['call_id']
    recording_callback(client,cid)
    async def audio(rid): return b'ID3-synthetic-audio'
    g.recording_audio=audio;m.settings.oauth_database_path=tmp_path/'oauth.sqlite3'
    server,_=create_mcp(m)
    async def run():
        meta=await server.call_tool('get_call_recordings',{'call_id':cid})
        assert not meta.is_error and RID in meta.content[0].text
        result=await server.call_tool('get_call_recording_audio',{'call_id':cid,'recording_id':RID})
        assert not result.is_error
        block=next(c for c in result.content if c.type=='audio')
        assert base64.b64decode(block.data)==b'ID3-synthetic-audio'
    asyncio.run(run())


@pytest.mark.parametrize('forwarded,expected',[(None,None),('+12025550109',True),('anonymous',True)])
def test_forwarding_preserves_original_caller_and_never_leaks_owner_number(service,forwarded,expected):
    client,m,_=service;m.settings.inbound_missed_call_context=True;m.settings.inbound_mode='concierge'
    m.settings.owner_transfer_number='+12025550109'
    extra={'ForwardedFrom':forwarded} if forwarded is not None else {}
    incoming(client,m,From='+12025550101',**extra)
    cid=m.db.by_twilio_sid(SID)['call_id'];record=m.voice_record(cid)
    assert record['phone_number']=='+12025550101'
    assert m.result(cid)['forwarding']['detected']==expected
    config=session_config(m.settings,record)
    missed=forwarded==m.settings.owner_transfer_number
    assert ('MISSED-CALL ASSISTANT' in config['instructions'])==missed
    assert '+12025550109' not in json.dumps(config)
    assert ('declined, is busy' in config['instructions'])==missed


@pytest.mark.parametrize('forwarded,policy,arrival,missed',[
    ('+12025550109',True,'forwarded_from_owner_mobile',True),
    ('+12025550108',True,'forwarded_other',False),
    (None,True,'direct_or_unreported',False),
    ('anonymous',True,'forwarded_other',False),
    ('+12025550109',False,'forwarded_from_owner_mobile',False)])
def test_call_specific_missed_routing_reaches_voice_backend_and_opening(service,forwarded,policy,arrival,missed):
    client,m,_=service;m.settings.inbound_mode='concierge'
    m.settings.inbound_missed_call_context=policy;m.settings.owner_transfer_number='+12025550109'
    incoming(client,m,**({'ForwardedFrom':forwarded} if forwarded else {}))
    cid=m.db.by_twilio_sid(SID)['call_id'];record=m.voice_record(cid)
    routing=record['incoming_context']['forwarding']
    assert routing['arrival_type']==arrival and routing['missed_call_context']==missed
    assert 'forwarded_from' not in routing
    assert m.result(cid)['forwarding']['arrival_type']==arrival
    config=session_config(m.settings,record)
    for prompt in (config['instructions'],config['delegation']['responses']['instructions']):
        assert ('MISSED-CALL ASSISTANT' in prompt)==missed
        assert ('CALL ARRIVAL: '+arrival in prompt)==(not missed)
    opening=config['input'][0]['content'][0]['text']
    assert ('could not take the call' in opening)==missed
    assert m.settings.owner_transfer_number not in json.dumps(config)


def test_carrier_replacing_caller_with_forwarding_number_does_not_impersonate_owner(service):
    client,m,_=service;m.settings.caller_id_trust_saved_contacts=True
    m.knowledge.contact(ContactUpdate(name='Owner',phone_number='+12025550101',conversation_style='familiar'))
    incoming(client,m,From='+12025550101',ForwardedFrom='+12025550101')
    context=m.voice_record(m.db.by_twilio_sid(SID)['call_id'])['incoming_context']
    assert not context['caller']['identity_verified'] and not context['caller']['saved_contacts']
    assert context['forwarding']['caller_identity_ambiguous']


def test_media_redirect_does_not_receive_provider_credentials(service,monkeypatch):
    import httpx
    from app.calling import recordings
    _,m,_=service;seen=[];real_client=httpx.AsyncClient
    def receive(request):
        seen.append(request)
        if len(seen)==1: return httpx.Response(302,headers={'location':'https://media.twiliocdn.com/synthetic.mp3'})
        return httpx.Response(200,content=b'ID3-synthetic-audio')
    monkeypatch.setattr(recordings.httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(receive),**kwargs))
    assert asyncio.run(recordings.fetch_recording_audio(m.settings,RID))==b'ID3-synthetic-audio'
    assert seen[0].headers.get('authorization') and 'authorization' not in seen[1].headers


@pytest.mark.parametrize('location',['http://127.0.0.1/secret','https://untrusted.example/audio','https://api.twilio.com:444/secret'])
def test_media_redirects_cannot_target_arbitrary_hosts(service,monkeypatch,location):
    import httpx
    from app.calling import recordings
    _,m,_=service;seen=[];real_client=httpx.AsyncClient
    def receive(request):
        seen.append(request)
        return httpx.Response(302,headers={'location':location})
    monkeypatch.setattr(recordings.httpx,'AsyncClient',lambda **kwargs:real_client(transport=httpx.MockTransport(receive),**kwargs))
    with pytest.raises(ValueError): asyncio.run(recordings.fetch_recording_audio(m.settings,RID))
    assert len(seen)==1
