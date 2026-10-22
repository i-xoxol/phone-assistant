"""Synthetic regressions for a single brief confirmation and prompt booking."""
import json
from datetime import timedelta
import pytest
from app.appointments.gateway import readback_text
from app.appointments.registry import appointment
from app.calling.live_session import MediaBridge
from app.calling.inbound import incoming_session_config
from tests.appointment_fixtures import NOW
from tests.appointment_fixtures import SLOT,FakeProvider
from app.appointments.gateway import AppointmentGateway
from app.appointments import confirmation
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,speech,agree


def collected(service,tmp_path):
    client,m,_=service;m.settings.appointment_database_path=tmp_path/'private.sqlite3'
    m.settings.appointments_enabled=True;m.settings.appointments_writes_enabled=True
    p=FakeProvider(lambda:NOW);g=AppointmentGateway(m,p,clock=lambda:NOW);m.appointments=g
    cid=m.db.create_inbound('CA_fixture','+12025550111',15,10,'concierge',True);m.transition(cid,'in_progress')
    with m.db.connect() as db:db.execute('update calls set created_at=?,started_at=? where call_id=?',(NOW.isoformat(),NOW.isoformat(),cid))
    for i,(who,text) in enumerate([
        ('assistant','What is your full name?'),('callee','Casey Example.'),
        ('assistant','What is the appointment for?'),('callee','Course guidance.'),
        ('assistant','How long would you like?'),('callee','Fifteen minutes.'),
        ('assistant','Which room?'),('callee','Office A.')]):speech(m,cid,who,text,i*1000)
    r=client.portal.call(g.check,cid,{'first_name':'Casey','last_name':'Example','purpose':'course guidance',
        'start':SLOT.start.isoformat(),'end':SLOT.end.isoformat(),'time_zone':SLOT.zone,'arrangement':'Office A'})
    return client,m,p,g,cid,r


@pytest.mark.parametrize('answer',['Okay.','Sounds good.','Yes, it is correct.','fragmented'])
def test_collected_details_need_one_short_time_confirmation_and_one_write(service,tmp_path,answer):
    client,m,p,g,cid,r=collected(service,tmp_path)
    d=g.store.get('appointment_drafts','draft_id',r['draft_id']);v=appointment(d)
    assert r['readback']=='October 26th, 10:00 AM. Is that correct?'
    assert '2026' not in r['readback'] and 'Eastern' not in r['readback']
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_BRIEF');m.attach_bridge(cid,bridge)
        await bridge.send_pending_updates();command=con.updates[-1][1]
        assert command['content'].endswith(confirmation.SHORT+r['readback'])
        # ACK timestamps are session-relative; stored captions and ACKs share the offset.
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id'],
            'start_ms':4500,'end_ms':4700})
        boundary=bridge.transcript_offset_ms+4700
        speech(m,cid,'assistant','Thank you, let me confirm that. ',boundary-1000)
        speech(m,cid,'assistant',r['readback'],boundary+1000)
        if answer=='fragmented':
            for delta,offset in [('Yes, ',2000),('it is ',2200),('correct.',2500)]:speech(m,cid,'callee',delta,boundary+offset)
        else:speech(m,cid,'callee',answer,boundary+2500)
        speech(m,cid,'assistant','Okay, booking it now.',boundary+3500)
        m.caller_activity[cid]={'epoch':1,'last_speech_ms':boundary+3000}
        item={'name':'book_appointment','call_id':'brief-one','arguments':json.dumps({
            'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'stable'})}
        result=await bridge.tools.operation(item)
        assert result['state']=='confirmed' and p.writes==1
        assert (await bridge.tools.operation(item))['operation_id']==result['operation_id'] and p.writes==1
        assert len(con.updates)==2  # One readback, one verified result, no repair/supervisor.
        assert 'Confirmed. October 26th, 10:00 AM.' in con.updates[-1][1]['content']
        assert '2026' not in con.updates[-1][1]['content'] and 'Eastern' not in con.updates[-1][1]['content']
        assert not g.delivery_status(cid,result['operation_id'])['spoken_observed']
        speech(m,cid,'assistant','Not confirmed. October 26th, 10:00 AM.',boundary+4500)
        assert not g.delivery_status(cid,result['operation_id'])['spoken_observed']
        speech(m,cid,'callee','What?',boundary+5500)
        speech(m,cid,'assistant','Confirmed. October 26th, 10:00 AM.',boundary+6500)
        speech(m,cid,'callee','Thank you.',boundary+7500)
        assert g.delivery_status(cid,result['operation_id'])['spoken_observed']
        assert g.delivery_status(cid,result['operation_id'])['caller_receipt_observed']
        assert m.db.get(cid)['supervisor_requests']==[]
        with g.store.connect() as db:assert db.execute('select count(*) from appointment_checks').fetchone()[0]==1
    client.portal.call(exercise)


@pytest.mark.parametrize('guard',['unknown_details','changed_name','changed_room','changed_duration','refusal','pending_audio','missing_ack','before_ack','overlap','before_ack_correction'])
def test_short_confirmation_keeps_complete_payload_and_current_speech_guards(service,tmp_path,guard):
    client,m,p,g,cid,r=collected(service,tmp_path)
    with g.store.connect() as db:rb=dict(db.execute('select * from appointment_readbacks').fetchone())
    if guard!='missing_ack':
        with m.db.connect() as db:db.execute("update call_updates set status='acknowledged',end_ms=? where update_id=?",(20000 if guard=='before_ack' else 9000,rb['update_id']))
    speech(m,cid,'assistant',r['readback'],10000)
    speech(m,cid,'callee','Okay.',10200 if guard=='overlap' else 11000)
    if guard=='unknown_details':
        with m.db.connect() as db:db.execute("update transcript_events set text='Maybe.' where call_id=? and text='Fifteen minutes.'",(cid,))
    if guard=='changed_name':speech(m,cid,'callee','Actually, my name is Jordan Else.',12000)
    if guard=='before_ack_correction':speech(m,cid,'callee','Actually, my name is Jordan Else.',8000)
    if guard=='changed_room':speech(m,cid,'callee','Actually, Office B instead.',12000)
    if guard=='changed_duration':speech(m,cid,'callee','Thirty minutes instead.',12000)
    if guard=='refusal':speech(m,cid,'callee','No, do not book it.',12000)
    if guard=='pending_audio':m.caller_activity[cid]={'epoch':2,'last_speech_ms':13000}
    with pytest.raises(ValueError):client.portal.call(g.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'stable'})
    assert p.writes==0


@pytest.mark.parametrize('change',['October 27th, 10:00 AM','October 26th, 11:10 AM','October 26th, 10:00 PM',
    'October 26th 2027, 10:00 AM','October 26th, 10:00 AM Pacific','October 26th, 10:00 AM or 11:00 AM'])
def test_short_parser_compares_the_whole_date_and_time(service,tmp_path,change):
    client,m,p,g,cid,r=collected(service,tmp_path)
    v=appointment(g.store.get('appointment_drafts','draft_id',r['draft_id']))
    assert confirmation.matches(r['readback'],v,short=True)
    assert not confirmation.matches(change+'. Is that correct?',v,short=True)


@pytest.mark.parametrize('field,text',[('duration','One hour and fifteen minutes.'),('duration','One hundred fifteen minutes.'),
    ('location','Not Office A.'),('purpose','Maybe course guidance.'),('purpose','Course guidance for my friend.')])
def test_omitted_fields_need_unambiguous_current_answers(service,tmp_path,field,text):
    client,m,p,g,cid,r=collected(service,tmp_path)
    v=appointment(g.store.get('appointment_drafts','draft_id',r['draft_id']))
    turns=m.db.get(cid)['transcript']
    assert confirmation.established_details(turns,v)
    questions={'duration':'How long?','location':'Which room?','purpose':'What is the appointment for?'}
    turns=turns+[{'speaker':'assistant','text':questions[field],'timestamp':10,'end_timestamp':10.5},
        {'speaker':'callee','text':text,'timestamp':11,'end_timestamp':11.5}]
    assert not confirmation.established_details(turns,v)


@pytest.mark.parametrize('change',['Office B.','Thirty minutes.','A different course.','Make it online.'])
def test_unelicited_substantive_change_requires_reading_remaining_details(service,tmp_path,change):
    client,m,p,g,cid,r=collected(service,tmp_path)
    v=appointment(g.store.get('appointment_drafts','draft_id',r['draft_id']))
    turns=m.db.get(cid)['transcript']
    assert confirmation.established_details(turns,v)
    turns=turns+[{'speaker':'assistant','text':r['readback'],'timestamp':10,'end_timestamp':10.5},
        {'speaker':'callee','text':change,'timestamp':11,'end_timestamp':11.5},
        {'speaker':'assistant','text':'Is that correct?','timestamp':12,'end_timestamp':12.5},
        {'speaker':'callee','text':'Yes.','timestamp':13,'end_timestamp':13.5}]
    assert not confirmation.established_details(turns,v)


def test_unestablished_details_are_spoken_once_without_year_or_zone(service,tmp_path):
    client,m,p,g,cid,r=prepare(service,tmp_path)
    assert 'Casey Example' in r['readback'] and '15 minutes' in r['readback'] and 'Office A' in r['readback']
    assert '2026' not in r['readback'] and 'Eastern' not in r['readback'] and 'America/' not in r['readback']


def test_repeated_check_cannot_change_the_already_queued_script(service,tmp_path):
    client,m,p,g,cid,r=prepare(service,tmp_path)
    for i,(who,text) in enumerate([
        ('assistant','What is your full name?'),('callee','Casey Example.'),
        ('assistant','What is the appointment for?'),('callee','Course guidance.'),
        ('assistant','How long?'),('callee','Fifteen minutes.'),
        ('assistant','Which room?'),('callee','Office A.')]):speech(m,cid,who,text,i*1000)
    result=client.portal.call(g.check,cid,{'request_id':r['request_id'],'first_name':'Casey','last_name':'Example',
        'purpose':'course guidance','start':SLOT.start.isoformat(),'end':SLOT.end.isoformat(),
        'time_zone':SLOT.zone,'arrangement':'Office A'})
    assert result['draft_id']==r['draft_id'] and result['readback']==r['readback']
    assert len(m.db.get(cid)['call_updates'])==1
    with m.db.connect() as db:db.execute("update call_updates set status='acknowledged' where call_id=?",(cid,))
    # Even with established details, a model cannot swap in an unissued short script.
    speech(m,cid,'assistant','October 26th, 10:00 AM. Is that correct?',10000)
    speech(m,cid,'callee','Okay.',11000)
    with pytest.raises(ValueError):client.portal.call(g.book,cid,{'draft_id':r['draft_id'],'check_id':result['check_id'],'idempotency_key':'one'})
    assert p.writes==0


@pytest.mark.parametrize('elapsed,zone,expected',[(1,'America/New_York',True),(30,'America/New_York',True),
    (31,'America/New_York',False),(0,'America/New_York',False),(1,'America/Los_Angeles',False)])
def test_omitted_year_zone_only_for_unique_near_term_office_date(service,tmp_path,elapsed,zone,expected):
    from dataclasses import replace
    client,m,p,g,cid,r=prepare(service,tmp_path);v=appointment(g.store.get('appointment_drafts','draft_id',r['draft_id']))
    v=replace(v,interval=replace(v.interval,start=NOW+timedelta(days=elapsed),end=NOW+timedelta(days=elapsed,minutes=15),zone=zone))
    assert confirmation.eligible(v,NOW,g.service.policy.zone)==expected


@pytest.mark.parametrize('status',['Okay, booking it now.','Booking it.','Okay, booking it.'])
def test_delayed_preface_and_booking_status_do_not_discard_exact_assent(service,tmp_path,status):
    client,m,p,g,cid,r=prepare(service,tmp_path)
    d=g.store.get('appointment_drafts','draft_id',r['draft_id']);v=appointment(d)
    with g.store.connect() as db:rb=db.execute('select * from appointment_readbacks').fetchone()
    with m.db.connect() as db:db.execute("update call_updates set status='acknowledged',start_ms=1800,end_ms=2000 where update_id=?",(rb['update_id'],))
    # These late captions belong to speech before the instruction was accepted.
    speech(m,cid,'assistant','Thank you, let me confirm that. ',1000)
    speech(m,cid,'assistant','Casey Example, '+readback_text(v),2500)
    speech(m,cid,'callee','Yes, it is correct.',4000)
    speech(m,cid,'assistant',status,5000)
    before=len(m.db.get(cid)['call_updates'])
    result=client.portal.call(g.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    assert result['state']=='confirmed' and p.writes==1
    assert len(m.db.get(cid)['call_updates'])==before+1  # Only the verified result.


def test_appointment_prompt_has_no_supervisor_only_booking_route(service,tmp_path):
    client,m,p,g,cid,r=prepare(service,tmp_path);m.settings.appointments_enabled=True
    config=incoming_session_config(m.settings,m.db.get(cid))
    for text in [config['instructions'],config['delegation']['responses']['instructions']]:
        assert 'rescheduling or an actual booking, request supervisor help' not in text
        assert 'or a booking needs supervisor action' not in text
        assert 'Collect only missing details' in text


def test_update_ack_uses_same_timeline_as_captions_after_reconnect(service,tmp_path):
    client,m,p,g,cid,r=prepare(service,tmp_path)
    speech(m,cid,'assistant','Earlier conversation.',10000)
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_RECONNECT')
        m.attach_bridge(cid,bridge);await bridge.send_pending_updates()
        event=con.updates[-1][1]['event_id']
        bridge.update_event({'type':'session.instructions.appended','client_event_id':event,'start_ms':100,'end_ms':300})
        row=m.db.get(cid)['call_updates'][-1]
        assert row['start_ms']==bridge.transcript_offset_ms+100
        assert row['end_ms']==bridge.transcript_offset_ms+300
    client.portal.call(exercise)
