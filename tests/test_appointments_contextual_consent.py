"""Natural agreement remains bound to one complete exact proposal; synthetic calls only."""
import json
from datetime import timedelta
import pytest
from app.appointments.evidence import affirmative_answer,complete_readback
from app.appointments.gateway import readback_text
from app.appointments.registry import appointment
from app.calling.live_session import MediaBridge
from tests.appointment_fixtures import setup,NOW,SLOT
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,speech,agree


@pytest.mark.parametrize('text',[
    'Sounds good.', 'That works for me.', 'Okay.', 'Sure.', 'Go ahead.', 'Yeah, that sounds great.',
    'Yes, that is correct. Yes.', 'That is right. Absolutely correct. Thank you.',
    'Yes please. Thanks.', 'Perfect, thank you.', 'That will work for me.', 'Please book it.',
    'I want you to book the appointment.', "I'd like you to schedule that appointment.",
    'Yes, it’s right.', 'Так, правильно.', 'Sounds good to me.', 'That would be great.',
    "Let's do it.", 'You can book this appointment.', "That's fine with me."])
def test_clear_contextual_agreement_needs_no_prescribed_phrase(text):
    assert affirmative_answer(text)


@pytest.mark.parametrize('text',[
    '', 'Thank you.', 'Please.', 'Maybe.', 'I think so.', 'Sounds good?', 'Okay?',
    'Yes, but tomorrow instead.', 'Yes, no, wait.', 'Okay, not that date.', 'Go ahead and cancel it.',
    'That works for my friend.', 'Sure, thirty minutes.', 'No, thanks.', 'Yes if it is online.',
    'Not correct.', 'I want you to book a different appointment.', 'Yes, unless it costs money.'])
def test_uncertainty_refusal_correction_or_new_condition_is_not_agreement(text):
    assert not affirmative_answer(text)


@pytest.mark.parametrize('prefix',['Okay, ','All right. ','Sure. ','Thank you. '])
def test_polite_readback_prefix_cannot_hide_wrong_fields(tmp_path,prefix):
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    text=prefix+'Casey Example, '+readback_text(v)
    assert complete_readback(text,v)
    for old,new in [('10:00 AM','11:10 AM'),('October 26','October 27'),('15 minutes','115 minutes'),('Office A','Office AB')]:
        assert not complete_readback(text.replace(old,new),v)
    assert not complete_readback('Not '+text,v)


@pytest.mark.parametrize('answer',['Sounds good.','That works for me.','Okay.','Go ahead.','Yes, that is correct. Yes.'])
def test_natural_agreement_books_once_without_another_readback(service,tmp_path,answer):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);m.settings.appointments_enabled=True
    v=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    text='Okay, Casey Example, '+readback_text(v)
    speech(m,cid,'assistant',text[:35],1000);speech(m,cid,'assistant',text[35:],1500)
    speech(m,cid,'callee',answer,2500)
    # A status or same-proposal follow-up must not discard the observed answer.
    speech(m,cid,'assistant','Great. Just a moment. Would you like me to book the appointment?',3500)
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_CONTEXT');m.attach_bridge(cid,bridge)
        item={'name':'book_appointment','call_id':'context-book','arguments':json.dumps({
            'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'stable'})}
        result=await bridge.tools.operation(item)
        assert result['state']=='confirmed' and p.writes==1
        assert (await bridge.tools.operation(item))['operation_id']==result['operation_id'] and p.writes==1
        with gateway.store.connect() as db:assert db.execute('select count(*) from appointment_readbacks').fetchone()[0]==1
        assert len(con.updates)==1 and 'Verified booking.' in con.updates[0][1]['content']
        assert m.db.get(cid)['supervisor_requests']==[]
    client.portal.call(exercise)


@pytest.mark.parametrize('followup',[
    'Did you need parking?', 'Would you like me to cancel it?', 'Actually October 27 at 11:10 AM.',
    'Not Office A.', 'Thirty minutes instead.', 'Are you available at eleven?',
    "Okay. I'll book that for your friend.", 'Does that work for your friend?'])
def test_unrelated_question_or_changed_proposal_cannot_borrow_agreement(service,tmp_path,followup):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    speech(m,cid,'assistant',followup,3000);speech(m,cid,'callee','Okay.',4000)
    with pytest.raises(ValueError):client.portal.call(gateway.book,cid,{
        'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'stable'})
    assert p.writes==0


def test_repeated_check_reuses_agreement_without_repeating_or_resetting_proof(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    v=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    # Another authorized request can refer to the same proposal, as a supervisor does.
    native=m.db.create_supervisor_request(cid,'Synthetic ordinary appointment',(NOW+timedelta(seconds=75)).isoformat())
    rid=native['request_id']
    before=m.db.get(cid)['call_updates']
    result=client.portal.call(gateway.check,cid,{'request_id':rid,'first_name':v.first_name,'last_name':v.last_name,
        'purpose':v.purpose,'start':v.interval.start.isoformat(),'end':v.interval.end.isoformat(),
        'time_zone':v.interval.zone,'arrangement':v.location})
    assert result['decision']=='available' and result['consent_observed'] and not result['readback_queued']
    assert result['expires_at']<=native['expires_at']
    assert m.db.get(cid)['call_updates']==before and p.writes==0
    assert client.portal.call(gateway.book,cid,{'draft_id':result['draft_id'],'check_id':result['check_id'],
        'idempotency_key':'stable'})['state']=='confirmed' and p.writes==1


@pytest.mark.parametrize('guard',['pending_audio','withdrawal','unrelated_question','changed_payload'])
def test_repeated_check_cannot_reuse_invalid_or_different_proof(service,tmp_path,guard):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    d=gateway.store.get('appointment_drafts','draft_id',r['draft_id'])
    if guard=='pending_audio':m.caller_activity[cid]={'epoch':1,'last_speech_ms':3000}
    if guard=='withdrawal':speech(m,cid,'callee','No, do not book it.',3000)
    if guard=='unrelated_question':
        speech(m,cid,'assistant','Would you like parking?',3000);speech(m,cid,'callee','Yes',4000)
    if guard=='changed_payload':d={**d,'payload_hash':'different'}
    assert not gateway.reuse_readback(d) and p.writes==0


@pytest.mark.parametrize('elapsed,expected_reuse',[(40,True),(46,False)])
def test_handoff_never_renews_original_consent_authority(service,tmp_path,elapsed,expected_reuse):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    now=[NOW+timedelta(seconds=elapsed)]
    gateway.clock=lambda:now[0];gateway.service.clock=gateway.clock;p.clock=gateway.clock
    native=m.db.create_supervisor_request(cid,'Synthetic ordinary appointment',(NOW+timedelta(seconds=150)).isoformat())
    v=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    result=client.portal.call(gateway.check,cid,{'request_id':native['request_id'],'first_name':v.first_name,'last_name':v.last_name,
        'purpose':v.purpose,'start':v.interval.start.isoformat(),'end':v.interval.end.isoformat(),
        'time_zone':v.interval.zone,'arrangement':v.location})
    assert result['consent_observed']==expected_reuse and result['readback_queued']!=expected_reuse
    if expected_reuse:
        assert result['expires_at']==(NOW+timedelta(seconds=45)).isoformat()
        now[0]=NOW+timedelta(seconds=46)
    with pytest.raises(ValueError):client.portal.call(gateway.book,cid,{'draft_id':result['draft_id'],
        'check_id':result['check_id'],'idempotency_key':'stable'})
    assert p.writes==0


def test_new_yes_after_refusal_requires_a_new_complete_readback(tmp_path):
    from app.appointments.evidence import spoken_consent
    s,db,p,d,state,now=setup(tmp_path);v=appointment(d)
    sequence=[('assistant','Casey Example, '+readback_text(v)),('callee','No.'),
        ('assistant','Would you like me to book it?'),('callee','Okay.')]
    turns=[{'speaker':speaker,'text':text,'start_ms':i*1000,'end_ms':i*1000+500} for i,(speaker,text) in enumerate(sequence)]
    assert spoken_consent(turns,v)==(False,False)
