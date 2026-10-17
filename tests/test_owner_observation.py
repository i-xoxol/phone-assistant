import asyncio
import base64
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect

from app.calling.context import utcnow
from app.calling.knowledge import FactUpdate
from app.calling.lookup import Selection
from app.calling.transcription import AudioTurns,pcm24
from tests.test_calls import service
from tests.test_owner_transfer import setup,callback,CHILD,OWNER
from tests.test_inbound import SID
from tests.test_live import stream_headers


def prepared(service):
    client,m,g=service
    cid,bridge=setup(client,m,g)
    m.settings.owner_observation_enabled=True
    m.settings.subscription_lookup_enabled=True
    async def ring(row,seconds):
        assert row['owner_number']==OWNER and seconds<=300
        g.rings=getattr(g,'rings',0)+1
        return SimpleNamespace(sid=CHILD)
    g.ring_owner=ring
    g.whispers=[]
    async def whisper(cf,sid,url): g.whispers.append((cf,sid,url))
    g.whisper=whisper
    tid=asyncio.run(m.transfer.request(cid,'A complex question needs the owner.'))['transfer_id']
    return client,m,g,cid,tid


def cb(client,m,tid,action,**values):
    return callback(client,tid,action,**{'From':m.settings.twilio_phone_number,**values})


@pytest.mark.parametrize('digit,mode',[('1','whisper'),('3','transcript')])
def test_both_choices_keep_two_microphone_streams_and_pin_first_mode(service,digit,mode):
    client,m,g,cid,tid=prepared(service)
    assert g.rings==1
    assert '<Conference' in g.redirects[0][1] and 'track="inbound_track"' in g.redirects[0][1]
    assert 'startConferenceOnEnter="false"' in g.redirects[0][1]
    screen=cb(client,m,tid,'screen')
    assert 'Press 1' in screen.text and 'press 3' in screen.text and 'Press 2' in screen.text
    accepted=cb(client,m,tid,'accept',Digits=digit)
    assert accepted.status_code==200 and '<Conference' in accepted.text and '<Start><Stream' in accepted.text
    assert 'startConferenceOnEnter="true"' in accepted.text and 'record="do-not-record"' in accepted.text
    assert m.transfer.get(tid)['mode']==mode
    cb(client,m,tid,'accept',Digits='3' if digit=='1' else '1')
    assert m.transfer.get(tid)['mode']==mode and g.rings==1
    assert OWNER not in json.dumps(m.result(cid))


@pytest.mark.parametrize('state',['no-answer','busy','failed','canceled','completed'])
def test_failed_or_unaccepted_independent_owner_leg_resumes_assistant(service,state):
    client,m,g,cid,tid=prepared(service)
    result=cb(client,m,tid,'leg-status',CallStatus=state)
    assert result.status_code==200
    assert m.transfer.get(tid)['status']=='returned_to_assistant'
    assert '<Stop><Stream name="owner-caller"' in g.redirects[-1][1]
    assert '<Connect><Stream' in g.redirects[-1][1] and g.rings==1
    count=len(g.redirects)
    cb(client,m,tid,'leg-status',CallStatus=state)
    assert len(g.redirects)==count
    assert '<Hangup' in cb(client,m,tid,'accept',Digits='1').text


def test_decline_returns_caller_and_cannot_be_undone(service):
    client,m,g,cid,tid=prepared(service)
    assert '<Hangup' in cb(client,m,tid,'accept',Digits='2').text
    assert m.transfer.get(tid)['accepted']==0
    assert '<Hangup' in cb(client,m,tid,'accept',Digits='1').text
    assert g.rings==1


def test_wrong_owner_leg_or_conference_cannot_select_mode_or_receive_whispers(service):
    client,m,g,cid,tid=prepared(service)
    assert cb(client,m,tid,'accept',Digits='1',From='+12025550108').status_code==403
    assert cb(client,m,tid,'accept',Digits='1',CallSid='CA'+'9'*32).status_code==403
    assert cb(client,m,tid,'conference-status',CallSid=SID,ConferenceSid='CF'+'4'*32,FriendlyName='wrong',
        StatusCallbackEvent='conference-start').status_code==403


def test_conference_end_completes_both_sides_without_inbox_fallback(service):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='3')
    values={'CallSid':SID,'ConferenceSid':'CF'+'4'*32,'FriendlyName':m.observer.room(m.transfer.get(tid))}
    assert cb(client,m,tid,'conference-status',StatusCallbackEvent='conference-start',**values).status_code==200
    assert cb(client,m,tid,'conference-status',StatusCallbackEvent='conference-end',**values).status_code==200
    assert m.transfer.get(tid)['status']=='completed' and m.db.get(cid)['status']=='completed'
    m.db.human_caption(cid,'human-test','callee',100,200,'A conversation, not an inbox message.')
    m.capture_inbound_fallback(cid)
    assert m.db.get(cid)['message_id'] is None


def test_captions_revise_in_place_and_preserve_owner_speaker(service):
    client,m,g,cid,tid=prepared(service)
    m.db.human_caption(cid,'human-owner-1','owner',100,200,'Tues')
    m.db.human_caption(cid,'human-owner-1','owner',100,400,'Tuesday morning.')
    result=m.result(cid)
    assert result['transcript']==[{'source_event_id':'human-owner-1','speaker':'owner','timestamp':.1,
        'end_timestamp':.4,'text':'Tuesday morning.'}]


def test_transcript_mode_never_starts_luna(service):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='3')
    async def forbidden(*_): raise AssertionError('Luna must stay off')
    m.observer.runner=forbidden
    async def notify():
        m.observer.transcript_changed(tid)
        assert not m.observer.coaches
    asyncio.run(notify())
    assert not g.whispers


def test_whisper_uses_only_selected_current_fact_and_only_owner_participant(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    m.transfer.update(tid,conference_sid='CF'+'4'*32)
    fid=uuid4()
    m.knowledge.fact(FactUpdate(fact_id=fid,topic='Test',text='The blue bicycle arrives Tuesday.',
        source='Owner test',approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1)))
    async def select(question,facts,timeout):
        return Selection(selected_ids=[str(fid)],needs_supervisor=False)
    async def no_delay(_): pass
    monkeypatch.setattr('app.calling.owner_observer.asyncio.sleep',no_delay)
    m.observer.runner=select
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==1 and g.whispers[0][:2]==('CF'+'4'*32,CHILD)
    action=g.whispers[0][2].rsplit('/',1)[1]
    assert 'blue bicycle' in cb(client,m,tid,action).text
    assert cb(client,m,tid,action,CallSid=SID).status_code==403
    m.knowledge.retire(str(fid))
    assert 'blue bicycle' not in cb(client,m,tid,action).text


def test_audio_vad_commits_speech_not_silence_and_pcm_has_expected_length():
    messages=[]
    async def send(data): messages.append(data)
    connection=SimpleNamespace(send=send)
    async def run():
        turns=AudioTurns()
        for _ in range(5): await turns.feed(connection,b'\xff'*160)
        assert not messages
        for _ in range(6): await turns.feed(connection,b'\x00'*160)
        for _ in range(40): await turns.feed(connection,b'\xff'*160)
    asyncio.run(run())
    assert sum(x['type']=='input_audio_buffer.commit' for x in messages)==1
    assert len(pcm24(b'\xff'*160))==960


def test_unsigned_and_wrong_call_microphone_cannot_connect_to_transcription(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='3')
    def forbidden(_): raise AssertionError('Must validate before connecting provider')
    monkeypatch.setattr('app.calling.transcription.live_client',forbidden)
    path='/twilio/owner-media/'+tid+'/owner'
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path): pass
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':SID,'streamSid':'MZ_OWNER',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert m.db.get(cid)['status']=='in_progress'


def test_transcription_failure_keeps_human_call_connected_and_reports_degraded(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='3')
    def unavailable(_): raise RuntimeError('private provider message')
    monkeypatch.setattr('app.calling.transcription.live_client',unavailable)
    path='/twilio/owner-media/'+tid+'/owner'
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':CHILD,'streamSid':'MZ_OWNER',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert m.transfer.get(tid)['transcription_state']=='degraded'
    assert m.db.get(cid)['status']=='in_progress' and g.status=='in-progress'
    assert 'private provider' not in json.dumps(m.result(cid))


def test_whisper_provider_failure_does_not_redial_or_repeat_announcement(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    m.transfer.update(tid,conference_sid='CF'+'4'*32)
    note=FactUpdate(topic='Demo',text='Demo parcels go to reception.',source='Owner test',
        approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1))
    m.knowledge.fact(note)
    async def select(*_): return Selection(selected_ids=[str(note.fact_id)],needs_supervisor=False)
    async def no_delay(_): pass
    async def ambiguous(cf,sid,url):
        g.whispers.append((cf,sid,url));raise TimeoutError('secret provider message')
    monkeypatch.setattr('app.calling.owner_observer.asyncio.sleep',no_delay)
    m.observer.runner=select;g.whisper=ambiguous
    asyncio.run(m.observer.coach(tid));asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==1 and g.rings==1
    assert m.db.get(cid)['status']=='in_progress'


def test_signed_microphone_stream_persists_revised_owner_captions_without_luna(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='3')
    class Connection:
        def __init__(self): self.queue=asyncio.Queue();self.sent=False
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        def __aiter__(self): return self
        async def __anext__(self):
            value=await self.queue.get()
            return SimpleNamespace(model_dump=lambda:value)
        async def send(self,data):
            if data['type']=='session.update': await self.queue.put({'type':'session.updated'})
            if data['type']=='input_audio_buffer.append' and not self.sent:
                self.sent=True
                for value in ({'type':'conversation.item.input_audio_transcription.delta','item_id':'test','delta':'Tuesday'},
                    {'type':'conversation.item.input_audio_transcription.completed','item_id':'test','transcript':'Tuesday morning.'}):
                    await self.queue.put(value)
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        realtime=SimpleNamespace(connect=lambda **_:Connection())
    monkeypatch.setattr('app.calling.transcription.live_client',lambda _:Client())
    path='/twilio/owner-media/'+tid+'/owner'
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':CHILD,'streamSid':'MZ_OWNER',
            'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        ws.send_json({'event':'media','streamSid':'MZ_OWNER','media':{'track':'inbound',
            'payload':base64.b64encode(b'\x00'*160).decode()}})
        ws.send_json({'event':'stop','streamSid':'MZ_OWNER'})
        with pytest.raises(WebSocketDisconnect): ws.receive_json()
    assert m.result(cid)['transcript'][-1]['speaker']=='owner'
    assert m.result(cid)['transcript'][-1]['text']=='Tuesday morning.'
    assert not m.observer.coaches and not g.whispers
