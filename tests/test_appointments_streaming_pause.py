"""A trailing streamed hold phrase must not request the same spoken consent again."""
import json
import pytest
from tests.appointment_fixtures import setup,SLOT
from tests.test_calls import service
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech
from tests.test_appointments_calendar_details import google_saved
from app.appointments.registry import appointment
from app.appointments.gateway import readback_text
from app.appointments.evidence import spoken_consent


def sequence(value,pause):
    return [dict(speaker=who,text=text,start_ms=i*1000,end_ms=i*1000+500)
        for i,(who,text) in enumerate([('assistant','Casey Example, '+readback_text(value)),
            ('callee','That works for me.'),('assistant',pause)])]


@pytest.mark.parametrize('pause',['Give me','Okay, give me a','Great. Give me one','Okay, give me just a'])
def test_trailing_pause_fragment_preserves_already_observed_exact_agreement(tmp_path,pause):
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    assert spoken_consent(sequence(v,pause),v)==(True,True)


@pytest.mark.parametrize('change',[
    'Okay, give me a different room.', 'Okay, give me a moment. October 27th at 11 AM instead.',
    'Give me a different duration.', 'Give me a moment. Would you like parking?',
    'Give me a?', 'Give me a moment for your friend.',
])
def test_completed_change_question_or_ambiguity_cannot_preserve_agreement(tmp_path,change):
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    assert spoken_consent(sequence(v,change),v)==(False,False)


def test_pause_fragment_is_only_allowed_after_existing_assent_and_as_final_streamed_block(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    turns=sequence(v,'Okay, give me a')
    turns[1]['text']='No, do not book it.'
    assert spoken_consent(turns,v)==(False,False)
    turns=sequence(v,'Okay, give me a');turns.pop(1)
    assert spoken_consent(turns,v)==(False,False)
    turns=sequence(v,'Okay, give me a')
    turns.append(dict(speaker='callee',text='Yes.',start_ms=3000,end_ms=3500))
    assert spoken_consent(turns,v)==(False,False)


@pytest.mark.parametrize('tool_name',['check_appointment_slot','find_first_available_slot'])
@pytest.mark.parametrize('change',['none','pending_audio','withdrawal','different_room_during_read'])
def test_voice_books_once_after_agreement_and_streaming_pause_without_a_second_proposal(service,tmp_path,tool_name,change):
    from app.calling.live_session import MediaBridge
    from tests.test_call_updates import UpdateConnection
    client,m,fake,g,cid,args=first_call(service,tmp_path)
    p,saved,requests=google_saved();g.service.provider=p
    if tool_name=='check_appointment_slot':
        args.pop('duration_minutes');args.update(start=SLOT.start.isoformat(),end=SLOT.end.isoformat(),time_zone=SLOT.zone)
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_STREAM_PAUSE');m.attach_bridge(cid,bridge)
        checked=await bridge.tools.operation({'name':tool_name,'arguments':json.dumps(args)})
        command=con.updates[0][1]
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
        speech(m,cid,'assistant',command['content'].split('\n')[-1],20000)
        speech(m,cid,'callee','That works for me.',22000)
        speech(m,cid,'assistant','Okay, give me a',24000)
        if change=='pending_audio':m.caller_activity[cid]={'last_speech_ms':26000}
        if change=='withdrawal':speech(m,cid,'callee','Actually, do not book it.',26000)
        if change=='different_room_during_read':
            original=p.read_interval
            async def changed(interval,exclude=None):
                speech(m,cid,'assistant',' different room.',25000)
                return await original(interval,exclude)
            p.read_interval=changed
        item={'name':'book_appointment','arguments':json.dumps(dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))}
        result=await bridge.tools.operation(item)
        if change=='none':
            assert result['state']=='confirmed' and len(saved)==1 and len(con.updates)==2
            assert (await bridge.tools.operation(item))['operation_id']==result['operation_id']
            assert len(con.updates)==2 and sum(r.method=='POST' for r in requests)==1
            assert not m.db.get(cid)['supervisor_requests']
        else:
            assert result['state']!='confirmed' and not saved and not any(r.method=='POST' for r in requests)
        await p.http.aclose()
    client.portal.call(exercise)
