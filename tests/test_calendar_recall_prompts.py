import asyncio,json
from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace
import pytest
from app.calling.context import utcnow
from app.calling.audio_timing import InputAudioTimeline,current_caller_audio
from app.calling.gemini_session import GeminiConnection
from app.calling.lookup import RecallSelection
from app.calling.memory import return_scope
from app.calling.prompt_catalog import system_prompt_catalog
from app.storage.models import CallRequest,CallUpdateRequest
from tests.test_calls import service
from tests.test_call_memory import caption,NUMBER
from tests.test_lookup import setup_call
from tests.test_voice_backends import connection as eleven_connection,translated
from tests.test_gemini_session import conn as gemini_connection,translate


def seed(m,minutes=2,number=NUMBER,text='My name is Casey Example. We discussed the spring classes.'):
    cid=m.db.create(CallRequest(phone_number=number,objective='Fictional follow-up'))
    m.transition(cid,'completed');stamp=utcnow()-timedelta(minutes=minutes)
    with m.db.connect() as db:db.execute('UPDATE calls SET created_at=?,completed_at=? WHERE call_id=?',(stamp.isoformat(),stamp.isoformat(),cid))
    caption(m,cid,'callee',text,start=1000)
    caption(m,cid,'assistant','We were checking a possible meeting; no booking was verified.',start=2000)
    return cid


def test_provider_sample_clock_mapping_preserves_gaps_and_future_speech():
    clock=InputAudioTimeline()
    clock.observe(7000,160,True);clock.observe(7020,160,True)
    clock.observe(9000,160,True)
    assert clock.stream_time(20)==7020 and clock.stream_time(50)==9010
    assert clock.covered_time(100000) is None
    manager=SimpleNamespace(caller_activity={'c':{'last_speech_ms':9020,'provider_timing_required':True,'caption_covered_ms':7040}})
    assert not current_caller_audio(manager,'c',[{'speaker':'callee','end_timestamp':999}])
    manager.caller_activity['c']['caption_covered_ms']=9020
    assert current_caller_audio(manager,'c',[])


def test_timestamp_free_asr_cannot_credit_a_newer_untranscribed_utterance():
    c=InputAudioTimeline()
    c.observe(7000,8000,True);c.observe(8000,4000,False)
    first=c.transcription_span()
    assert first==(7000,8000)
    c.finish_transcription()
    c.observe(8500,8000,True);c.observe(9500,4000,False)
    assert c.transcript_coverage()==8000
    c.transcription_span()
    assert c.transcript_coverage()==9500


def test_caption_coverage_waits_for_speech_to_close():
    c=InputAudioTimeline();c.observe(10000,8000,True)
    assert c.transcription_span()==(10000,None)
    assert c.transcript_coverage() is None
    c.observe(11000,4000,False)
    assert c.transcript_coverage()==11000


def test_timestamp_free_caption_never_extends_to_future_speech_automatically():
    c=InputAudioTimeline();c.observe(7000,8000,True);c.transcription_span()
    c.observe(8000,8000,True);c.observe(9000,4000,False)
    assert c.transcript_coverage()==8000


@pytest.mark.parametrize('case',['match','mismatch','new_speech','hangup','no_key'])
def test_independent_caption_proof_requires_matching_text_and_unchanged_audio(case):
    from app.calling.caption_verification import CaptionVerification
    c=InputAudioTimeline();v=CaptionVerification(c)
    for start,raw,spoken in [(7000,b'\x80'*8000,True),(8000,b'\xff'*4000,False),
        (8500,b'\x80'*8000,True),(9500,b'\xff'*4000,False)]:
        c.observe(start,len(raw),spoken);v.observe(raw)
    text='My name is Casey Example. Please check my appointment.'
    record={'status':'in_progress','direction':'inbound','inbound_mode':'concierge','transcript':[{'speaker':'callee','text':text}]}
    m=SimpleNamespace(stopping=set(),bridges={},appointments=None,caller_activity={'c':{'last_speech_ms':9500,'caption_covered_ms':8000}},
        settings=SimpleNamespace(appointments_enabled=True,openai_api_key=SimpleNamespace(get_secret_value=lambda:'' if case=='no_key' else 'fake')),
        db=SimpleNamespace(get=lambda _:record))
    b=SimpleNamespace(manager=m,call_id='c',closing=False,handoff=False,end_requested=asyncio.Event(),transcript_offset_ms=0)
    m.bridges['c']=b
    async def transcribe(*_):
        if case=='new_speech':m.caller_activity['c']['last_speech_ms']=11000
        if case=='hangup':record['status']='completed'
        return "No, don't book anything." if case=='mismatch' else text
    v.transcribe=transcribe
    async def run():
        await v.verify(b)
        assert m.caller_activity['c']['caption_covered_ms']==(9500 if case=='match' else 8000)
        await v.close();assert not v.frames
    asyncio.run(run())


@pytest.mark.parametrize('ratio',[1,2,3])
def test_native_clock_calibrates_to_captured_speech_and_excludes_new_answer(ratio):
    c=InputAudioTimeline()
    c.observe(7000,32000,False);c.observe(11000,12000,True);c.observe(12500,4000,False)
    assert c.native_span(4000/ratio,5800/ratio)==(11000,12500)
    c.observe(13000,800,True)
    # A late caption of the old answer cannot cover this correction, even if
    # the native endpoint includes silence padding into the new speech.
    assert c.native_span(4000/ratio,6100/ratio)[1]==12500
    m=SimpleNamespace(caller_activity={'c':{'provider_timing_required':True,'last_speech_ms':13100,'caption_covered_ms':12500}})
    assert not current_caller_audio(m,'c',[])


@pytest.mark.parametrize('ratio',[1,2,3])
def test_reconnect_while_caller_is_already_speaking_calibrates_endpoint(ratio):
    c=InputAudioTimeline();c.observe(20000,16000,True);c.observe(22000,8000,False)
    assert c.native_span(0,2100/ratio)==(20000,22000)
    assert c.native_scale==ratio


def test_elevenlabs_final_caption_uses_audio_not_provider_wall_clock():
    async def run():
        c=eleven_connection()
        for tick in range(100):c.observe_input_audio(7000+tick*20,b'\xff'*160)
        await translated(c,{'type':'input_audio_buffer.speech_started','item_id':'i','audio_start_ms':200})
        await translated(c,{'type':'input_audio_buffer.speech_stopped','item_id':'i','audio_end_ms':1200})
        events=await translated(c,{'type':'conversation.item.input_audio_transcription.completed','item_id':'i','transcript':'Casey Example'})
        assert events[0]['start_ms']==7200 and events[0]['end_ms']==8200
        assert events[0]['input_audio_end_ms']==8200
    asyncio.run(run())


def test_gemini_committed_asr_uses_captured_utterance_clock():
    async def run():
        c=gemini_connection()
        # Non-silence G.711 codes followed by a quiet endpoint.
        c.observe_input_audio(7000,b'\x80'*8000);c.observe_input_audio(8000,b'\xff'*4000)
        event=(await translate(c,{'server_content':{'input_transcription':{'text':'My name is Casey Example.'}}}))[0]
        assert event['start_ms']==7000 and event['input_audio_end_ms']==8000
        c.input_timeline.finish_transcription();c.observe_input_audio(8500,b'\x80'*8000)
        assert c.input_caption_coverage()==8000
    asyncio.run(run())


def test_calendar_spoken_instruction_triggers_response_without_fabricating_ack():
    async def run():
        c=gemini_connection()
        await c.append_instruction('Offer the verified fictional appointment.', 'call_update_calendar',respond=True)
        assert c.native.contents[0]['turn_complete'] is True
        assert (await c.queue.get()).type=='session.update_unconfirmed'
        e=eleven_connection()
        await e.append_instruction('Offer the verified fictional appointment.','call_update_calendar',respond=True)
        assert any(v['type']=='response.create' for v in e.socket.sent)
        assert e.pending_instructions
        event=e.socket.sent[0]
        ack=await translated(e,{'type':'session.updated','session':event['session']})
        assert ack[0]['type']=='session.instructions.appended'
    asyncio.run(run())


@pytest.mark.parametrize('cue,scope',[("I just called",'recent'),("it's me again",'recent'),('I called today','today'),
    ('I called yesterday about classes','yesterday'),('I called several days ago about my classes','days'),('Thanks again',None)])
def test_return_cues(cue,scope):assert return_scope(cue.casefold())==scope


def test_recent_callback_loads_name_and_topic_automatically_and_once(service):
    client,m,_=service;m.settings.call_history_enabled=True
    prior=seed(m)
    cid,b=setup_call(client,m)
    calls=[]
    async def select(question,facts,timeout):
        calls.append(question);return RecallSelection(selected_ids=[f['fact_id'] for f in facts[:8]])
    m.memory.runner=select
    async def run():
        caption(m,cid,'callee',"It's me again, I just called.")
        m.memory.observe(cid,b)
        await asyncio.gather(*list(m.memory.tasks.values()))
        state=m.memory.snapshot(cid)
        assert state['scope']=='recent' and state['state']=='ready' and state['source_calls']==[prior]
        content=' '.join(u['content'] for u in m.db.get(cid)['call_updates'])
        assert 'Casey Example' in content and 'spring classes' in content
        assert 'within ten minutes' in content and 'not current booking proof' in content
        assert NUMBER not in content
        m.memory.observe(cid,b)
        assert not m.memory.tasks and len(calls)==1
    asyncio.run(run())


def test_today_context_is_richer_than_two_fragments_and_excludes_other_numbers(service):
    client,m,_=service;m.settings.call_history_enabled=True
    a=seed(m,minutes=60);seed(m,number='+12025550109',text='Unrelated private visitor.')
    for i in range(8):caption(m,a,'callee',f'The fictional course detail number {i} was discussed.',start=3000+i*1000)
    cid,b=setup_call(client,m)
    facts=m.memory.recall_candidates(m.db.get(cid),'today')
    assert len(facts)>2 and 'Casey Example' in str(facts) and 'Unrelated' not in str(facts)
    assert not m.memory.recall_candidates(m.db.get(cid),'recent')


def test_yesterday_recall_requires_topic_and_filters_the_requested_day(service):
    client,m,_=service;m.settings.call_history_enabled=True
    yesterday=seed(m,minutes=1440);seed(m,minutes=3,text='This is an unrelated recent delivery.')
    cid,b=setup_call(client,m)
    facts=m.memory.recall_candidates(m.db.get(cid),'yesterday')
    assert facts and {f['source_call_id'] for f in facts}=={yesterday}
    async def run():
        caption(m,cid,'callee','I called yesterday.')
        m.memory.observe(cid,b);assert not m.memory.tasks
        caption(m,cid,'assistant','What was it about?',start=1000)
        caption(m,cid,'callee','The spring classes and the course meeting.',start=2000)
        m.memory.observe(cid,b)
        assert m.memory.tasks
        await asyncio.gather(*list(m.memory.tasks.values()))
        assert m.memory.snapshot(cid)['source_calls']==[yesterday]
    asyncio.run(run())


def test_ambiguous_forwarded_number_and_sensitive_owner_speech_stay_private(service):
    client,m,_=service;m.settings.call_history_enabled=True
    previous=seed(m);m.db.human_caption(previous,str(uuid4()),'owner',4000,4500,'Secret owner coaching should never be recalled.')
    cid,b=setup_call(client,m)
    assert 'Secret owner coaching' not in str(m.memory.recall_candidates(m.db.get(cid),'recent'))
    with m.db.connect() as db:db.execute('UPDATE incoming_routing SET forwarded_from=?,forwarding_reported=1 WHERE call_id=?',(NUMBER,cid))
    assert not m.memory.recall_candidates(m.db.get(cid),'recent')


def test_history_never_becomes_current_booking_or_lookup_identity(service):
    client,m,_=service;m.settings.call_history_enabled=True;seed(m)
    cid,b=setup_call(client,m)
    from app.appointments.evidence import caller_identified
    assert not caller_identified(m.db.get(cid)['transcript'],'Casey','Example',for_lookup=True)


def test_system_prompts_are_owner_only_complete_and_do_not_call_providers(service):
    client,m,_=service
    catalog=system_prompt_catalog();text=' '.join(s['text'] for s in catalog['sections'])
    assert 'You handle appointment booking intent' in text
    assert 'Resolve an existing appointment name' in text
    assert 'PRIVATE coach' in text and 'return-call conversation' in text
    assert 'FIRST TURN' in text and 'CALENDAR INTENT' in text
    assert catalog['provider_requests'] is False
    assert client.get('/live/system-prompts').status_code==401
    assert client.get('/voice/system-prompts',headers={'Authorization':'Bearer wrong'}).status_code==401
    response=client.get('/voice/system-prompts');assert response.status_code==200
    assert not m.db.recent_calls()


def test_response_required_persists_and_is_part_of_update_identity(service):
    client,m,_=service
    cid=m.db.create(CallRequest(phone_number=NUMBER,objective='Fictional'))
    uid=str(uuid4());m.db.queue_update(cid,uid,'Speak the verified result.','instructions',True)
    assert m.db.call_update(cid,uid)['response_required']==1
    with pytest.raises(ValueError):m.db.queue_update(cid,uid,'Speak the verified result.','instructions',False)


def test_recall_reserves_updates_for_live_calendar_and_owner_work(service):
    client,m,_=service;m.settings.call_history_enabled=True;seed(m)
    cid,b=setup_call(client,m)
    for i in range(11):m.db.queue_update(cid,str(uuid4()),f'Existing owner context {i}','context')
    async def run():
        caption(m,cid,'callee',"It's me again, I just called.")
        async def select(*_):return RecallSelection(selected_ids=[])
        m.memory.runner=select;m.memory.observe(cid,b)
        await asyncio.gather(*list(m.memory.tasks.values()))
        assert len(m.db.get(cid)['call_updates'])==12
        for i in range(8):m.db.queue_update(cid,str(uuid4()),f'Calendar response {i}','instructions',True)
        assert len(m.db.get(cid)['call_updates'])==20
    asyncio.run(run())


@pytest.mark.parametrize('config',[None,{}, {'model':'gpt-live-1'}])
def test_mini_lookup_helper_leaves_other_voice_connections_alone(config):
    from app.appointments.lookup_intent import MiniLookupIntent
    g=SimpleNamespace(booking_agent=SimpleNamespace(client=None))
    intent=MiniLookupIntent(g)
    intent.notify(SimpleNamespace(call_id='c',connection=SimpleNamespace(config=config)))
    assert not intent.tasks


@pytest.mark.parametrize('case',['match','unproven_name','changed_audio','message_only','already_dispatched','new_booking'])
def test_mini_lookup_helper_uses_same_calendar_guards_and_never_writes(service,tmp_path,case):
    from tests.test_appointments_gateway import prepare,speech
    from tests.test_call_updates import UpdateConnection
    from app.calling.live_session import MediaBridge
    from app.appointments.lookup_intent import LookupIntent
    client,m,p,g,cid,proposal=prepare(service,tmp_path);m.settings.appointments_enabled=True
    c=UpdateConnection();c.config={'model':'eleven_realtime_v1_mini'}
    b=MediaBridge(None,c,m,cid,'MZ_MINI',luna_booking=True);m.attach_bridge(cid,b)
    speech(m,cid,'callee','Please check my appointment.' if case=='unproven_name' else
        'My name is Casey Example. Please check my appointment.',1000)
    reads=[];decisions=[]
    async def decide(turns):
        decisions.append(turns)
        if case=='changed_audio':m.caller_activity[cid]={'epoch':2,'last_speech_ms':30000}
        return LookupIntent(action='lookup',first_name='Casey',last_name='Example')
    # Use a fictional verified result without touching a provider.
    from tests.appointment_fixtures import SLOT
    async def read(first,last,*,contradictory=False):
        reads.append(contradictory)
        return {'status':'needs_owner'} if contradictory else {'status':'match','start':SLOT.start.isoformat(),
            'end':SLOT.end.isoformat(),'zone':SLOT.zone,'location':'Office A'}
    g.service.own_appointment=read;g.lookup_intent.decide=decide
    if case=='message_only':
        with m.db.connect() as db:db.execute("UPDATE calls SET inbound_mode='messages' WHERE call_id=?",(cid,))
    if case=='already_dispatched':g.last_voice_lookup[cid]=g.revision(cid)
    if case=='new_booking':g.voice_workflow.bind(cid,proposal)
    async def run():
        initial=len(m.db.get(cid)['call_updates'])
        await g.lookup_intent.run(b)
        updates=m.db.get(cid)['call_updates'][initial:]
        if case=='match':assert reads==[False] and len(updates)==1 and 'Office A' in updates[0]['content'] and updates[0]['response_required']
        elif case=='unproven_name':assert reads==[True] and 'Office A' not in str(updates)
        else:assert not reads and not updates
        assert p.writes==0
    client.portal.call(run)
