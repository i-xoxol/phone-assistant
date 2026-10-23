"""Replay native Live captions into the server-owned booking controller."""
import asyncio,json
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4
import pytest
from tests.test_calls import service
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech
from tests.test_appointments_calendar_details import google_saved
from tests.test_call_updates import UpdateConnection
from app.calling.live_session import MediaBridge
from app.appointments.contracts import CalendarEvent,EventIdentity
from app.appointments.evidence import affirmative_answer,spoken_consent
from app.appointments.gateway import readback_text
from app.appointments.registry import appointment
from app.appointments.schemas import VOICE_TOOLS
from tests.appointment_fixtures import SLOT,NOW,setup


class Captions(UpdateConnection):
    def __init__(self):super().__init__();self.events=[];self.consumed=asyncio.Event()
    def __aiter__(self):
        async def iterate():
            for event in self.events:
                yield SimpleNamespace(type=event['type'],model_dump=lambda e=event:e)
                await asyncio.sleep(0)
            self.consumed.set()
            await asyncio.Event().wait()  # A live connection stays open while booking finishes.
        return iterate()


def caption(who,text,start,end):
    return {'type':'session.input_transcript.delta' if who=='callee' else 'session.output_transcript.delta',
        'event_id':str(uuid4()),'delta':text,'start_ms':start,'end_ms':end}


async def drain(g,cid):
    task=g.voice_workflow.tasks.get(cid)
    if task:await asyncio.wait_for(asyncio.shield(task),2)


async def replay(bridge,connection):
    connection.consumed.clear();receiver=asyncio.create_task(bridge.receive_live())
    try:
        await asyncio.wait_for(connection.consumed.wait(),2)
        await drain(bridge.manager.appointments,bridge.call_id)
    finally:
        receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)


async def checked_voice(service,tmp_path,tool_name='find_first_available_slot',lose=False):
    client,m,fake,g,cid,args=first_call(service,tmp_path)
    provider,saved,requests=google_saved(lose=lose);g.service.provider=provider
    g.voice_workflow.settle_seconds=.01
    con=Captions();bridge=MediaBridge(None,con,m,cid,'MZ_SERVER_WORKFLOW');m.attach_bridge(cid,bridge)
    bridge.transcript_offset_ms=0  # Fixture intake is already on this stream's absolute timeline.
    if tool_name=='check_appointment_slot':
        args.pop('duration_minutes');args.update(start=SLOT.start.isoformat(),end=SLOT.end.isoformat(),time_zone=SLOT.zone)
    result=await bridge.tools.operation({'name':tool_name,'arguments':json.dumps(args)})
    command=con.updates[0][1]
    bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
    return m,g,cid,bridge,con,provider,saved,requests,result,command


def test_native_voice_book_and_status_have_no_model_selected_ids():
    for name in ('book_appointment','get_booking_status'):
        schema=next(t['parameters'] for t in VOICE_TOOLS if t['name']==name)
        assert schema['properties']=={} and not schema.get('required') and schema['additionalProperties'] is False


@pytest.mark.parametrize('answer',["Yeah, that's would work for me, thank you.",'This would work for me.','That sounds good to me.',
    'Uh, yeah, that time works for me.',"That'll work, thanks.",'I can make that.',"I'm good with that.",
    'That is okay with me.','Works for me.','I think that should work for me.','Well, sure, go ahead.'])
def test_complete_positive_clause_accepts_caption_disfluency_without_dropping_fields(answer):
    assert affirmative_answer(answer)
    for suffix in [' but tomorrow instead',' if it is online',' for my friend',' at eleven']:
        assert not affirmative_answer(answer+suffix)


@pytest.mark.parametrize('tail,accepted',[
    ("I'll take care of that for you.",True),('Give me one minute while I finish checking.',True),
    ('Just one moment while I finish that.',True),('Eleven.',False),('Twenty-one.',False),('Noon.',False),
    ('Give me a moment. Eleven instead.',False),('Give me a different room.',False),
    ("I'll book that for your friend.",False),('Does your friend want that?',False),
    ('Alice Smith at Oak Hall.',False),('We are moving to Oak Street.',False),
    ('I will take care of the appointment for Casey.',False),
])
def test_only_trailing_status_can_leave_exact_existing_agreement_intact(tmp_path,tail,accepted):
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    seq=[('assistant','Casey Example, '+readback_text(v)),('callee','Yes.'),('assistant',tail)]
    turns=[dict(speaker=who,text=t,start_ms=i*1000,end_ms=i*1000+500) for i,(who,t) in enumerate(seq)]
    assert spoken_consent(turns,v)==((True,True) if accepted else (False,False))


@pytest.mark.parametrize('tool_name',['find_first_available_slot','check_appointment_slot'])
@pytest.mark.parametrize('lose',[False,True])
def test_actual_failure_shape_creates_once_without_a_model_book_call_or_repeated_proposal(service,tmp_path,tool_name,lose):
    client,*_=service
    async def exercise():
        m,g,cid,b,con,p,saved,requests,check,command=await checked_voice(service,tmp_path,tool_name,lose)
        # This formerly raised KeyError for an invented operation and ended the call.
        status=await b.tools.operation({'name':'get_booking_status','arguments':json.dumps({'operation_id':str(uuid4())})})
        assert status['state']=='needs_consent' and not status['calendar_write_attempted'] and not saved
        m.caller_activity[cid]={'epoch':1,'last_speech_ms':24000}
        con.events=[caption('assistant',command['content'].split('\n')[-1],20000,21000),
            caption('callee',"Yeah, that's",22000,23000),caption('callee',' would work for me, thank you.',23200,24000),
            caption('assistant',"I'll take care of that for you.",25000,26000)]
        await replay(b,con)
        assert len(saved)==1 and sum(r.method=='POST' for r in requests)==1
        assert len(con.updates)==2  # One proposal, one verified result.
        assert '\nVerified booking.' in con.updates[-1][1]['content']
        status=await b.tools.operation({'name':'get_booking_status','arguments':'{}'})
        assert status['state']=='confirmed' and status['operation_id']
        again=await b.tools.operation({'name':'book_appointment','arguments':'{}'})
        assert again['operation_id']==status['operation_id'] and len(con.updates)==2
        assert not m.db.get(cid)['supervisor_requests']
        body=next(iter(saved.values()));assert 'Purpose: course guidance' in body['description']
        assert body['location']=='Office A' and body['visibility']=='private' and not body.get('attendees')
        await p.http.aclose()
    client.portal.call(exercise)


def test_caption_lag_never_asks_again_or_writes_until_the_complete_answer_arrives(service,tmp_path):
    client,*_=service
    async def exercise():
        m,g,cid,b,con,p,saved,requests,check,command=await checked_voice(service,tmp_path)
        m.caller_activity[cid]={'epoch':1,'last_speech_ms':24000}
        con.events=[caption('assistant',command['content'].split('\n')[-1],20000,21000),caption('callee','Yes',22000,23000)]
        await replay(b,con)
        assert not saved and len(con.updates)==1
        # The delayed final caption can change the answer. A prefix "Yes" is no authority.
        con.events=[caption('callee',', but tomorrow instead.',23200,24000)]
        await replay(b,con)
        assert not saved and len(con.updates)==1
        assert not any(r.method=='POST' for r in requests)
        await p.http.aclose()
    client.portal.call(exercise)


def test_ambiguous_generic_answer_gets_one_short_question_then_server_books(service,tmp_path):
    client,*_=service
    async def exercise():
        m,g,cid,b,con,p,saved,requests,check,command=await checked_voice(service,tmp_path)
        con.events=[caption('assistant',command['content'].split('\n')[-1],20000,21000),caption('callee','Maybe.',22000,23000)]
        await replay(b,con)
        assert not saved and len(con.updates)==2
        assert 'Ask only: Does that work for you?' in con.updates[-1][1]['content']
        assert 'October' not in con.updates[-1][1]['content']
        # Repeated observer notifications cannot repeat the clarification.
        g.voice_workflow.notify(b);await drain(g,cid);assert len(con.updates)==2
        con.events=[caption('assistant','Does that work for you?',24000,25000),caption('callee','Yes.',26000,27000)]
        await replay(b,con)
        assert len(saved)==1 and len(con.updates)==3 and sum(r.method=='POST' for r in requests)==1
        await p.http.aclose()
    client.portal.call(exercise)


@pytest.mark.parametrize('guard',['refusal','changed_date','third_party','hangup','pending_audio','write_disabled','different_room_during_read',
    'expired_request','busy_during_read','withdrawal_during_read'])
def test_server_controller_cannot_bypass_any_booking_guard(service,tmp_path,guard):
    client,*_=service
    async def exercise():
        m,g,cid,b,con,p,saved,requests,check,command=await checked_voice(service,tmp_path)
        answer={'refusal':'No, do not book it.','changed_date':'Yes, but tomorrow instead.','third_party':'That works for my friend.'}.get(guard,'Yes.')
        if guard=='hangup':m.transition(cid,'completed')
        if guard=='pending_audio':m.caller_activity[cid]={'last_speech_ms':26000}
        if guard=='write_disabled':m.settings.appointments_writes_enabled=False
        if guard=='expired_request':g.service.clock=g.clock=lambda:NOW+timedelta(minutes=1)
        if guard=='different_room_during_read':
            original=p.read_interval
            async def altered(interval,exclude=None):
                speech(m,cid,'assistant','Actually, Room B instead.',25000)
                return await original(interval,exclude)
            p.read_interval=altered
        if guard in ('busy_during_read','withdrawal_during_read'):
            original=p.read_interval
            async def altered(interval,exclude=None):
                coverage=await original(interval,exclude)
                if guard=='withdrawal_during_read':speech(m,cid,'callee','No, do not book it.',25000)
                else:coverage=replace(coverage,events=(*coverage.events,CalendarEvent(EventIdentity(p.identity,'new-conflict'),'rev',SLOT)))
                return coverage
            p.read_interval=altered
        con.events=[caption('assistant',command['content'].split('\n')[-1],20000,21000),caption('callee',answer,22000,23000)]
        await replay(b,con)
        assert not saved and not any(r.method=='POST' for r in requests)
        assert not any('\nVerified booking.' in x[1]['content'] for x in con.updates)
        await p.http.aclose()
    client.portal.call(exercise)


def test_pending_proposal_and_status_survive_controller_reconstruction(service,tmp_path):
    from app.appointments.workflow import VoiceBookingWorkflow
    client,*_=service
    async def exercise():
        m,g,cid,b,con,p,saved,requests,check,command=await checked_voice(service,tmp_path)
        g.voice_workflow=VoiceBookingWorkflow(g)
        assert g.voice_workflow.bound(cid)[0]['draft_id']==check['draft_id']
        status=await g.voice_workflow.status(cid)
        assert status['phase']=='awaiting_readback' and not status['calendar_write_attempted'] and not saved
        await p.http.aclose()
    client.portal.call(exercise)


def test_an_existing_operation_cannot_confirm_a_different_current_proposal(service,tmp_path):
    client,*_=service
    async def exercise():
        m,g,cid,b,con,p,saved,requests,check,command=await checked_voice(service,tmp_path)
        con.events=[caption('assistant',command['content'].split('\n')[-1],20000,21000),caption('callee','Yes.',22000,23000)]
        await replay(b,con);assert len(saved)==1
        native=m.db.create_supervisor_request(cid,'Synthetic changed proposal',(NOW+timedelta(seconds=40)).isoformat())
        result=await b.tools.operation({'name':'check_appointment_slot','arguments':json.dumps(dict(
            request_id=native['request_id'],first_name='Casey',last_name='Example',purpose='course guidance',
            arrangement='Office B',start=(SLOT.start+timedelta(minutes=30)).isoformat(),
            end=(SLOT.end+timedelta(minutes=30)).isoformat(),time_zone=SLOT.zone))})
        status=await b.tools.operation({'name':'get_booking_status','arguments':'{}'})
        assert status['state']=='needs_owner' and status['reason']=='existing_call_write_requires_reconciliation'
        resumed=await b.tools.operation({'name':'book_appointment','arguments':'{}'})
        assert resumed['state']=='needs_owner' and len(saved)==1 and sum(r.method=='POST' for r in requests)==1
        await p.http.aclose()
    client.portal.call(exercise)
