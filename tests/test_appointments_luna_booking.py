"""Luna interprets speech; the calendar handler does not re-parse its words."""
import asyncio
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from app.appointments.booking_agent import BookingAction,conversation
from app.appointments.registry import appointment
from app.appointments.schemas import VOICE_TOOLS,SlotRequest
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech
from tests.appointment_fixtures import NOW,SLOT


async def native(service,tmp_path):
    client,m,p,g,cid,args=first_call(service,tmp_path)
    con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_LUNA',luna_booking=True);m.attach_bridge(cid,bridge)
    g.voice_workflow.settle_seconds=.01
    seen=[]
    async def decide(turns,value,now):
        seen.append(turns)
        offer=next(t['turn'] for t in reversed(turns) if t['speaker']=='assistant')
        answer=next(t['turn'] for t in reversed(turns) if t['speaker']=='callee')
        return BookingAction(action='book',offer_turn=offer,answer_turn=answer)
    g.booking_agent.decide=decide
    result=await bridge.tools.operation(dict(name='find_first_available_slot',arguments=json.dumps({**args,'request_id':'invented-id'})))
    return m,p,g,cid,bridge,con,result,seen


def answer(m,cid,text='Well, I guess that sounds fine, yeah.'):
    # Both real failure shapes: harmless holding prefix and paraphrased intake.
    speech(m,cid,'assistant',"Still checking. Monday, October 26 at ten in the morning would work for your spring schedule. How's that?",20000)
    speech(m,cid,'callee',text,22000)


@pytest.mark.parametrize('words',['Well, I guess that sounds fine, yeah.','Uh, Monday works, thanks.',
    'Sure, whatever is free then.','I can do that one.','Yeah.',"That's alright, I think."])
def test_native_tool_trusts_luna_meaning_without_full_recital_or_exact_words(service,tmp_path,words):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path)
        assert p.writes==0 and len(con.updates)==1
        assert 'Monday, October 26th, 10:00 AM' in con.updates[0][1]['content']
        assert 'Casey' not in con.updates[0][1]['content'] and 'Office A' not in con.updates[0][1]['content']
        answer(m,cid,words)
        results=await asyncio.gather(b.tools.operation(dict(name='book_appointment',arguments='{}')),
            b.tools.operation(dict(name='book_appointment',arguments='{}')))
        assert all(r['state']=='confirmed' for r in results) and p.writes==1
        assert all('caller_delivery' not in r for r in results) and len(seen)==1
        assert words in str(seen[0])  # Luna gets the actual conversation, not a phrase whitelist.
        assert 'Great, see you Monday at 10:00 AM. Confirmed.' in con.updates[-1][1]['content']
        assert len(con.updates)==2
    client.portal.call(exercise)


@pytest.mark.parametrize('action',['decline','clarify','wait'])
def test_python_preserves_luna_refusal_or_unresolved_time_without_creating(service,tmp_path,action):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path);answer(m,cid,'No, make it eleven instead.')
        async def decide(*_):return BookingAction(action=action,offer_turn=None,answer_turn=None)
        g.booking_agent.decide=decide
        result=await b.tools.operation(dict(name='book_appointment',arguments='{}'))
        assert result['state']!='confirmed' and p.writes==0
    client.portal.call(exercise)


@pytest.mark.parametrize('kind',['out_of_range','wrong_speaker','before_offer'])
def test_bad_model_turn_references_cannot_authorize_a_write(service,tmp_path,kind):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path);answer(m,cid)
        async def decide(turns,*_):
            end=len(turns)-1
            return BookingAction(action='book',offer_turn=end+1 if kind=='out_of_range' else end if kind=='wrong_speaker' else end-1,
                answer_turn=end if kind!='before_offer' else 0)
        g.booking_agent.decide=decide
        assert (await b.tools.operation(dict(name='book_appointment',arguments='{}')))['state']!='confirmed'
        assert p.writes==0
    client.portal.call(exercise)


@pytest.mark.parametrize('kind',['pending_audio','changed_during_model','changed_during_provider','hangup','expired'])
def test_luna_decision_still_has_live_call_audio_and_dispatch_boundaries(service,tmp_path,kind):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path);answer(m,cid)
        if kind=='pending_audio':m.caller_activity[cid]={'epoch':1,'last_speech_ms':24000}
        if kind=='changed_during_model':
            original=g.booking_agent.decide
            async def decide(*args):
                decision=await original(*args);speech(m,cid,'callee','Actually, do not book.',24000);return decision
            g.booking_agent.decide=decide
        if kind=='changed_during_provider':p.read_hook=lambda:speech(m,cid,'callee','Actually, do not book.',24000)
        if kind=='hangup':m.transition(cid,'completed')
        if kind=='expired':
            with g.store.connect() as db:db.execute("UPDATE appointment_requests SET deadline='2000-01-01T00:00:00+00:00'")
        try:await b.tools.operation(dict(name='book_appointment',arguments='{}'))
        except ValueError:pass
        assert p.writes==0
    client.portal.call(exercise)


def test_native_slot_ids_are_server_owned_but_existing_mcp_schema_is_unchanged():
    for t in VOICE_TOOLS:
        if t['name'] in ('check_appointment_slot','find_first_available_slot'):
            assert 'request_id' not in t['parameters']['properties']
    assert 'request_id' in SlotRequest.model_json_schema()['properties']


def test_actual_luna_tool_decision_is_accepted_without_a_second_model_judge(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path);answer(m,cid,'Uh, yeah, whatever works.')
        context=b.tools.booking_context()
        async def forbidden(*_):raise AssertionError('A second intent judge must not run')
        g.booking_agent.decide=forbidden
        result=await b.tools.operation(dict(name='book_appointment',arguments=json.dumps({'invented_id':'ignored'}),_booking_context=context))
        assert result['state']=='confirmed' and p.writes==1 and not seen
        draft,_=g.voice_workflow.bound(cid)
        assert g.booking_agent.row(draft)['source']=='luna_tool_request'
    client.portal.call(exercise)


def test_stale_luna_request_rechecks_changed_caller_intent_instead_of_writing(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path);answer(m,cid)
        context=b.tools.booking_context();speech(m,cid,'callee','No, cancel that.',24000)
        async def decline(*_):return BookingAction(action='decline',offer_turn=None,answer_turn=None)
        g.booking_agent.decide=decline
        result=await b.tools.operation(dict(name='book_appointment',arguments='{}',_booking_context=context))
        assert result['state']=='declined' and p.writes==0
    client.portal.call(exercise)


def test_agent_outage_is_bounded_and_does_not_create_an_event(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path);answer(m,cid)
        async def unavailable(*_):return None
        g.booking_agent.decide=unavailable
        result=await b.tools.operation(dict(name='book_appointment',arguments='{}'))
        assert result['state']=='needs_owner' and p.writes==0
    client.portal.call(exercise)


def test_backend_cannot_book_an_unanswered_first_available_offer(service,tmp_path):
    client,*_=service
    async def exercise():
        m,p,g,cid,b,con,result,seen=await native(service,tmp_path)
        result=await b.tools.operation(dict(name='book_appointment',arguments='{}',_booking_context=b.tools.booking_context()))
        assert result['state']=='needs_consent' and p.writes==0 and not seen
    client.portal.call(exercise)
