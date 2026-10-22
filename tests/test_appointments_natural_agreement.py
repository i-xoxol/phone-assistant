"""Bounded conversational consent after an exact proposal, with real caption ordering."""
import json
import pytest
from app.appointments.evidence import affirmative_answer,spoken_consent
from app.appointments.registry import appointment
from tests.appointment_fixtures import setup,SLOT
from tests.test_calls import service
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech
from tests.test_appointments_calendar_details import google_saved


@pytest.mark.parametrize('answer',['Yes, that should work.','Yes, I agree with this appointment.'])
def test_ordinary_current_proposal_agreement_is_recognized(answer):
    assert affirmative_answer(answer)


@pytest.mark.parametrize('answer',[
    'Yes, that might work.','Yes, that could work.','That should work if I finish early.',
    "Yes, that won't work.",'I disagree with this appointment.','I agree with another appointment.',
    'I agree with this appointment tomorrow instead.','I agree with this appointment?',
    'Yes, I agree with this appointment, but change the room.','That should work, no, wait.',
])
def test_uncertain_conditional_or_changed_answers_are_not_consent(answer):
    assert not affirmative_answer(answer)


@pytest.mark.parametrize('followup',[
    'Great. Could you please confirm you agree to that appointment?',
    'Could you confirm that you agree to this appointment?',
])
def test_same_appointment_confirmation_question_keeps_exact_proposal(tmp_path,followup):
    from app.appointments.gateway import readback_text
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    texts=[('assistant',v.first_name+' '+v.last_name+', '+readback_text(v)),
        ('callee','Yes, that should work.'),('assistant',followup),('callee','Yes, I agree with this appointment.')]
    turns=[{'speaker':who,'text':text,'start_ms':i*1000,'end_ms':i*1000+500} for i,(who,text) in enumerate(texts)]
    assert spoken_consent(turns,v)==(True,True)
    for replacement in ['Could you confirm you agree to another appointment?',
        'Could you confirm you agree to that appointment at 11 AM?',
        'Could you confirm your friend agrees to that appointment?']:
        turns[2]['text']=replacement
        assert spoken_consent(turns,v)==(False,False)


@pytest.mark.parametrize('tool_name',['check_appointment_slot','find_first_available_slot'])
@pytest.mark.parametrize('followup',[False,True])
@pytest.mark.parametrize('withdrawal',[False,True])
def test_voice_caption_lag_then_natural_agreement_preserves_exact_booking(service,tmp_path,tool_name,followup,withdrawal):
    from app.calling.live_session import MediaBridge
    from tests.test_call_updates import UpdateConnection
    client,m,fake,g,cid,args=first_call(service,tmp_path)
    p,saved,requests=google_saved();g.service.provider=p
    if tool_name=='check_appointment_slot':
        args.pop('duration_minutes');args.update(start=SLOT.start.isoformat(),end=SLOT.end.isoformat(),time_zone=SLOT.zone)
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_NATURAL_AGREEMENT');m.attach_bridge(cid,bridge)
        checked=await bridge.tools.operation({'name':tool_name,'arguments':json.dumps(args)})
        command=con.updates[0][1]
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
        speech(m,cid,'assistant',command['content'].split('\n')[-1],20000)
        item={'name':'book_appointment','arguments':json.dumps(dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='stable'))}
        # Audio arrived but its final caption has not: do not authorize an early write.
        m.caller_activity[cid]={'last_speech_ms':22000}
        pending=await bridge.tools.operation(item)
        assert pending['state']=='needs_consent' and not pending['readback_queued'] and not saved
        speech(m,cid,'callee','Yes, that should work.',22000)
        if followup:
            speech(m,cid,'assistant','Great. Could you please confirm you agree to that appointment?',24000)
            speech(m,cid,'callee','Yes, I agree with this appointment.',26000)
        if withdrawal:speech(m,cid,'callee','Actually, do not book it.',28000)
        result=await bridge.tools.operation(item)
        if withdrawal:
            assert result['state']=='needs_consent' and not result['readback_queued'] and not saved
        else:
            assert result['state']=='confirmed' and len(saved)==1 and len(con.updates)==2
            assert (await bridge.tools.operation(item))['operation_id']==result['operation_id']
            event=next(iter(saved.values()))
            assert 'Purpose: course guidance' in event['description'] and event['location']=='Office A'
            assert event['visibility']=='private' and not event.get('attendees')
            assert sum(r.method=='POST' for r in requests)==1
        assert not m.db.get(cid)['supervisor_requests']
        await p.http.aclose()
    client.portal.call(exercise)
