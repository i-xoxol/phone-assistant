import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException

from app.calling.context import utcnow
from app.calling.knowledge import FactUpdate
from app.calling.owner_realtime import Channel,facts_key
from tests.test_calls import service
from tests.test_live import stream_headers
from tests.test_owner_observation import prepared,cb
from tests.test_owner_transfer import CHILD

COACH='CA'+'7'*32
CF='CF'+'4'*32


class FakeSession:
    instances=[]
    def __init__(self,settings,on_state):
        self.settings=settings;self.contexts=[];self.ready=asyncio.Event();self.ready.set()
        self.closed=asyncio.Event();self.cancelled=0
        self.instances.append(self)
    async def ask(self,context,on_audio,turns=None):
        self.contexts.append({**context,'audio_sources':[t['speaker'] for t in turns or []]})
        await on_audio(b'\x80'*640)
        return {'text':'You are free Monday from nine to ten Eastern.','audio_bytes':640,'skipped':False}
    async def cancel(self):self.cancelled+=1
    async def close(self):self.closed.set()


def ready(service,digit='1',app=True,owner_states=None,create_error=None):
    client,m,g,cid,tid=prepared(service)
    m.settings.owner_speech_enabled=True;m.settings.owner_coaching_strategy='realtime'
    m.settings.owner_coaching_application_sid='AP'+'1'*32 if app else ''
    m.settings.subscription_lookup_enabled=False  # No subscription worker needed for this mode.
    m.transfer.update(tid,conference_sid=CF)
    m.observer.realtime.factory=FakeSession
    g.coach_adds=[];g.coach_hangups=[];g.owner_checks=[]
    owner_states=list(owner_states or ['connected'])
    def participant():
        record=m.observer.realtime.record(tid)
        return SimpleNamespace(call_sid=COACH,conference_sid=CF,label='rt-'+record['coach_id'],
            coaching=True,call_sid_to_coach=CHILD)
    async def add(row,record,seconds):
        g.coach_adds.append((row,record,seconds))
        if create_error:raise create_error
        return participant()
    async def fetch(cf,label):
        assert cf==CF
        if label==CHILD:
            state=owner_states.pop(0) if len(owner_states)>1 else owner_states[0]
            g.owner_checks.append(state)
            if state=='missing':raise TwilioRestException(404,'/synthetic',code=20404,msg='Not joined yet')
            return SimpleNamespace(call_sid=COACH if state=='wrong_owner' else CHILD,conference_sid=CF,
                status='connected' if state=='held' else state,hold=state=='held')
        return participant()
    async def hangup(sid):g.coach_hangups.append(sid)
    g.add_coach=add;g.coach_participant=fetch;g.hangup=hangup
    def forbidden(*_,**__):raise AssertionError('Realtime coaching must bypass Luna and GPT-Live')
    m.observer.runner=forbidden;m.observer.coaching_runner=forbidden;m.observer.live.speak=forbidden
    cb(client,m,tid,'accept',Digits=digit)
    return client,m,g,cid,tid


def connect(client,m,tid,**extra):
    record=m.observer.realtime.record(tid)
    data={'AccountSid':m.settings.twilio_account_sid,'CallSid':COACH,
        'coach_id':record['coach_id'],'nonce':record['nonce'],**extra}
    path='/twilio/owner-coach/connect'
    sig=RequestValidator('twilio-test').compute_signature('https://example.test'+path,data)
    return client.post(path,data=data,headers={'X-Twilio-Signature':sig})


def start(m,tid,**changes):
    record=m.observer.realtime.record(tid)
    return {'event':'start','start':{'accountSid':'AC123','callSid':COACH,'streamSid':'MZ_COACH',
        'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1},
        'customParameters':{'coach_id':record['coach_id'],'nonce':record['nonce']},**changes}}


def caption(client,m,cid,tid,speaker,text,turn='test-turn'):
    async def apply():
        m.db.human_caption(cid,turn,speaker,1000,2000,text)
        m.observer.transcript_changed(tid,speaker,text,turn)
        if m.settings.owner_coaching_strategy=='realtime':
            m.observer.realtime.audio_turn(tid,'caller' if speaker=='callee' else speaker,b'\x80'*1600)
    client.portal.call(apply)


def fact(m):
    body=FactUpdate(topic='Availability',text='Owner is free Monday 9 to 10 AM Eastern. Nothing is booked.',
        source='Synthetic test',approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1))
    m.knowledge.fact(body)
    return body


def test_owner_can_accept_realtime_without_subscription_and_simple_mode_never_starts_models(service):
    client,m,g,cid,tid=ready(service,digit='3')
    assert m.transfer.get(tid)['mode']=='transcript'
    assert not m.observer.realtime.channels and not g.coach_adds
    caption(client,m,cid,tid,'owner','Rocky, what is my availability?')
    assert not m.observer.realtime.channels
    assert m.db.get(cid)['transcript'][-1]['text'].startswith('Rocky')


def test_missing_streaming_application_falls_back_to_captions(service):
    client,m,g,cid,tid=ready(service,app=False)
    assert m.transfer.get(tid)['mode']=='transcript'
    assert not g.coach_adds and not m.observer.realtime.channels


def test_coach_waits_for_the_actual_owner_to_join_not_just_the_room_or_acceptance(service):
    client,m,g,cid,tid=ready(service,owner_states=['missing','connecting','held','connected'])
    async def joined():
        async with asyncio.timeout(2):
            while not g.coach_adds:await asyncio.sleep(.01)
    client.portal.call(joined)
    assert g.owner_checks==['missing','connecting','held','connected']
    assert len(g.coach_adds)==1 and m.db.get(cid)['status']=='in_progress'


@pytest.mark.parametrize('state',['wrong_owner','complete','failed'])
def test_missing_or_wrong_owner_never_creates_a_coach_or_ends_the_human_call(service,state):
    client,m,g,cid,tid=ready(service,owner_states=[state])
    async def stopped():
        async with asyncio.timeout(1):
            while m.observer.realtime.record(tid)['status']!='failed':await asyncio.sleep(.01)
    client.portal.call(stopped)
    assert not g.coach_adds and not g.coach_hangups
    assert m.db.get(cid)['status']=='in_progress' and m.observer.realtime.ensure(tid) is None


def test_owner_leaving_during_join_wait_never_dials_a_coach(service):
    client,m,g,cid,tid=ready(service,owner_states=['connecting'])
    async def left():
        ch=m.observer.realtime.channels[tid]
        m.db.transition(cid,'completed')
        await ch.starter
    client.portal.call(left)
    assert not g.coach_adds and not g.coach_hangups
    assert m.observer.realtime.record(tid)['status']=='failed'


def test_definite_carrier_rejection_keeps_numeric_code_without_leaking_request_or_retrying(service):
    error=TwilioRestException(400,'/secret-request-url',code=16028,msg='secret-token-must-not-leak')
    client,m,g,cid,tid=ready(service,create_error=error)
    async def stopped():
        async with asyncio.timeout(1):
            while m.observer.realtime.record(tid)['status']!='failed':await asyncio.sleep(.01)
    client.portal.call(stopped)
    record=m.observer.realtime.record(tid)
    assert record['error_code']=='twilio_16028' and len(g.coach_adds)==1
    assert not g.coach_hangups and m.db.get(cid)['status']=='in_progress'
    assert m.observer.realtime.ensure(tid) is None
    with m.db.connect() as db:
        events=[r['payload'] for r in db.execute("SELECT payload FROM call_events WHERE type='call.owner_coaching'")]
    assert any(json.loads(event).get('twilio_code')==16028 for event in events)
    assert 'secret' not in json.dumps(events)


def test_proactive_caller_question_streams_privately_without_luna_and_persists_playback(service):
    client,m,g,cid,tid=ready(service)
    fact(m)
    assert m.transfer.get(tid)['mode']=='whisper'
    assert connect(client,m,tid).status_code==200
    path='/twilio/owner-coach/media/'+tid
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json(start(m,tid))
        caption(client,m,cid,tid,'callee','What is your availability on Monday?','caller-q')
        packets=[ws.receive_json(),ws.receive_json()]
        assert all(p['event']=='media' and p['streamSid']=='MZ_COACH' for p in packets)
        mark=ws.receive_json();assert mark['event']=='mark'
        ws.send_json(mark)
        context=m.observer.realtime.channels[tid].session.contexts[-1]
        assert context['trigger']=='consider_proactive_hint' and context['explicit_owner_request'] is None
        assert context['recent_captions'][-1]['speaker']=='callee'
        assert context['CURRENT_APPROVED_FACTS'][0]['text'].startswith('Owner is free')
        assert 'phone_number' not in json.dumps(context)
        ws.send_json({'event':'stop','streamSid':'MZ_COACH'})
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    assert m.observer.history(cid)[-1]['text']=='You are free Monday from nine to ten Eastern.'
    assert len(g.coach_adds)==1 and not g.whispers
    assert m.db.get(cid)['status']=='in_progress' and g.coach_hangups==[COACH]


def test_coach_twiml_and_stream_require_valid_signatures_correct_leg_and_private_routing(service):
    client,m,g,cid,tid=ready(service)
    assert client.post('/twilio/owner-coach/connect',data={}).status_code==403
    assert connect(client,m,tid,nonce='wrong').status_code==403
    response=connect(client,m,tid)
    assert response.status_code==200 and '<Connect><Stream' in response.text
    assert '/twilio/owner-coach/media/'+tid in response.text
    assert '<Dial>' not in response.text
    path='/twilio/owner-coach/media/'+tid
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(path):pass
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json(start(m,tid,callSid=CHILD))
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    assert m.db.get(cid)['status']=='in_progress'
    original=g.coach_participant
    async def public_coach(*args):
        value=await original(*args);value.coaching=False;return value
    g.coach_participant=public_coach
    assert connect(client,m,tid).status_code==403


def test_owner_let_me_check_cue_and_explicit_request_have_both_speaker_context(service):
    client,m,g,cid,tid=ready(service)
    fact(m);assert connect(client,m,tid).status_code==200
    path='/twilio/owner-coach/media/'+tid
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json(start(m,tid))
        m.db.human_caption(cid,'caller-first','callee',0,500,'Could we meet next week?')
        caption(client,m,cid,tid,'owner','Let me check my availability for next week.','owner-check')
        while (packet:=ws.receive_json())['event']!='mark':pass
        ws.send_json(packet)
        session=m.observer.realtime.channels[tid].session
        assert [c['speaker'] for c in session.contexts[-1]['recent_captions']]==['callee','owner']
        caption(client,m,cid,tid,'owner','Rocky, what about Monday?','owner-explicit')
        while (packet:=ws.receive_json())['event']!='mark':pass
        ws.send_json(packet)
        assert session.contexts[-1]['audio_sources'][-1]=='owner'
        assert len(session.contexts)==2
        ws.send_json({'event':'stop','streamSid':'MZ_COACH'})


def test_only_owner_can_pause_and_caller_cannot_resume_coaching(service):
    client,m,g,cid,tid=ready(service)
    async def pause():
        m.observer.transcript_changed(tid,'owner','Rocky, stop coaching.','stop')
        await asyncio.sleep(.02)
    client.portal.call(pause)
    assert tid in m.observer.paused
    caption(client,m,cid,tid,'callee','Rocky, start coaching again.','attacker')
    assert tid in m.observer.paused
    assert not m.observer.realtime.channels[tid].session.contexts


def test_fresh_facts_are_reloaded_and_revoked_context_invalidates_pending_stream(service):
    client,m,g,cid,tid=ready(service)
    body=fact(m);pool=m.observer.realtime;ch=pool.channels[tid]
    first=pool.context(tid,None)
    assert pool.valid(tid,ch,ch.generation,facts_key(first['CURRENT_APPROVED_FACTS']))
    m.knowledge.retire(str(body.fact_id))
    assert not pool.valid(tid,ch,ch.generation,facts_key(first['CURRENT_APPROVED_FACTS']))
    assert not pool.context(tid,None)['CURRENT_APPROVED_FACTS']


def test_ambiguous_coach_creation_is_never_retried_and_never_hangs_up_humans(service):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    m.settings.owner_coaching_strategy='realtime';m.settings.owner_speech_enabled=True
    m.settings.owner_coaching_application_sid='AP'+'1'*32
    m.transfer.update(tid,conference_sid=CF)
    pool=m.observer.realtime;pool.factory=FakeSession;attempts=[]
    async def ambiguous(*_):attempts.append(1);raise TimeoutError()
    async def absent(cf,label):
        if label==CHILD:return SimpleNamespace(call_sid=CHILD,conference_sid=CF,status='connected',hold=False)
        raise RuntimeError('provider unavailable')
    g.add_coach=ambiguous;g.coach_participant=absent
    async def run():
        ch=pool.ensure(tid)
        await ch.starter
        assert pool.record(tid)['status']=='unconfirmed'
        assert pool.ensure(tid) is None
    client.portal.call(run)
    assert attempts==[1] and m.db.get(cid)['status']=='in_progress'


def test_fact_updates_wake_native_coach_without_any_subscription_or_external_lookup(service):
    client,m,g,cid,tid=ready(service)
    pool=m.observer.realtime;ch=pool.channels[tid]
    ch.audio_history=[{'speaker':'caller','audio':b'last question'}]
    generation=ch.generation
    response=client.put('/concierge/facts',json={'topic':'New information','text':'Monday is unavailable now.',
        'source':'Synthetic owner update','approved_for_callers':True,
        'expires_at':(utcnow()+timedelta(hours=1)).isoformat()})
    assert response.status_code==200
    assert ch.generation>generation and ch.pending_audio
    assert any(f['text']=='Monday is unavailable now.' for f in pool.context(tid,None)['CURRENT_APPROVED_FACTS'])


def test_paused_audio_is_not_replayed_to_the_model_on_resume(service):
    client,m,g,cid,tid=ready(service)
    pool=m.observer.realtime;ch=pool.channels[tid]
    async def run():
        ch.audio_history=[{'speaker':'caller','audio':b'before pause'}]
        m.observer.paused.add(tid)
        await pool.pause(tid)
        pool.audio_turn(tid,'caller',b'private paused caller speech')
        pool.audio_turn(tid,'owner',b'private paused owner speech')
        assert not ch.audio_history
        pool.audio_turn(tid,'owner',b'latest verified resume request')
        m.observer.paused.discard(tid)
        pool.labels_changed(tid,resumed=True)
        assert ch.audio_history==[{'speaker':'owner','audio':b'latest verified resume request'}]
    client.portal.call(run)


def test_gateway_creates_only_a_private_application_coach_and_cannot_end_human_conference():
    from app.calling.twilio_client import TwilioGateway
    from app.config import Settings
    settings=Settings(_env_file=None,twilio_phone_number='+12025550100',
        owner_coaching_application_sid='AP'+'1'*32)
    gateway=TwilioGateway(settings);calls=[]
    def create(**kwargs):calls.append(kwargs);return SimpleNamespace(call_sid=COACH)
    gateway.client=lambda:SimpleNamespace(conferences=lambda cf:SimpleNamespace(
        participants=SimpleNamespace(create=create)))
    asyncio.run(gateway.add_coach({'conference_sid':CF,'child_sid':CHILD},
        {'coach_id':'synthetic-coach','nonce':'synthetic-nonce'},120))
    params=calls[0]
    assert params['to'].startswith('app:AP') and params['call_sid_to_coach']==CHILD
    assert params['coaching'] is True and params['end_conference_on_exit'] is False
    assert params['record'] is False and params['time_limit']==120
