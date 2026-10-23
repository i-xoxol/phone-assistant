"""Existing-appointment lookup accepts a complete current name without booking intake."""
import json
from uuid import uuid4
import pytest
from app.appointments.evidence import caller_identified
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,agree,speech


def turns(*pairs):
    return [dict(speaker=speaker,text=text,timestamp=i,end_timestamp=i+.4) for i,(speaker,text) in enumerate(pairs)]


@pytest.mark.parametrize('question,answers',[
    ('Your first and last name, please?', [('callee','Yes, Casey Example')]),
    ('Your full name?', [('callee',"Yes, it's Casey Example.")]),
    ('Your first name?', [('callee','Yes, Casey'),('assistant','And your surname?'),('callee','Yes, Example')]),
])
def test_acknowledgment_is_not_part_of_an_elicited_name(question,answers):
    assert caller_identified(turns(('assistant',question),*answers),'Casey','Example')


def test_a_pause_after_yes_does_not_discard_the_elicited_name_answer():
    rows=[dict(speaker=speaker,text=text,timestamp=start,end_timestamp=start+.2)
        for speaker,text,start in [('assistant','Your first and last name?',1),('callee','Yes,',3),
            ('callee',' Casey Ex',4.2),('callee','ample',5)]]
    assert not caller_identified(rows[:2],'Casey','Example')
    assert caller_identified(rows,'Casey','Example')


@pytest.mark.parametrize('reply',[
    "No, I- I want to check um, I want to clarify with you my time. I don't really remember. What was it? I just called.",
    'No-no, just I want to confirm to see my date and time of my appointment.',
    "Actually, I don't remember the appointment time.",
])
def test_a_request_to_retrieve_the_time_does_not_replace_a_complete_name(reply):
    assert caller_identified(turns(('callee','My name is Casey Example.'),
        ('assistant','Could you share the date and time of that appointment?'),('callee',reply)),'Casey','Example')


@pytest.mark.parametrize('reply',[
    'Yes, Casey', 'Yes, Example', 'Yes, Casey Exampleton', 'Yes, my friend Casey Example',
    'Yes, Casey Example and Jordan Else', 'Yes, not Casey Example', 'Actually, Jordan Else', "Wait, I'm",
])
def test_polite_prefix_cannot_supply_missing_superseded_or_third_party_identity(reply):
    assert not caller_identified(turns(('callee','My name is Casey Example.'),
        ('assistant','Your full name?'),('callee',reply)),'Casey','Example')


@pytest.mark.parametrize('reply',[
    'No, Casey', 'Actually, Jordan Else', 'No, I am Jordan Else.', 'Actually, my name is Casey.',
    "No, not Casey Example", "I'm calling about my appointment; my name is Jordan Else.",
])
def test_recall_followup_cannot_hide_an_identity_correction(reply):
    assert not caller_identified(turns(('callee','My name is Casey Example.'),
        ('assistant','What date and time do you recall?'),('callee',reply)),'Casey','Example')


@pytest.mark.parametrize('later_recall',[False,True])
def test_native_lookup_after_a_booking_retrieves_only_verified_details_without_any_new_write(service,tmp_path,later_recall):
    client,m,p,g,cid,result=prepare(service,tmp_path);agree(m,g,cid,result)
    m.settings.appointments_enabled=True
    booked=client.portal.call(g.book,cid,dict(draft_id=result['draft_id'],check_id=result['check_id'],idempotency_key='one'))
    assert booked['state']=='confirmed' and p.writes==1
    m.transition(cid,'completed')
    lookup=m.db.create_inbound('CA_'+uuid4().hex,'+12025550111',15,10,'concierge',True)
    m.transition(lookup,'in_progress')
    async def exercise():
        connection=UpdateConnection();bridge=MediaBridge(None,connection,m,lookup,'MZ_LOOKUP');m.attach_bridge(lookup,bridge)
        # Streamed surname fragments, followed by an unnecessarily repeated name question.
        for speaker,text,start in [('callee','Hi, my name is Casey Ex',1000),('callee','ample. I want to confirm my appointment.',1500),
                ('assistant','Your first and last name, please?',2500),('callee','Yes,',3500),('callee',' Casey Ex',5000),('callee','ample',5500)]:
            speech(m,lookup,speaker,text,start)
        if later_recall:
            speech(m,lookup,'assistant','What date and time do you have in mind?',6500)
            speech(m,lookup,'callee',"No, I want to clarify my time. I don't really remember.",7500)
        answer=await bridge.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Casey',last_name='Example'))})
        assert answer['status']=='match'
        assert set(answer)=={'status','start','end','zone','location','duration_seconds','current_delay_notices','spoken_answer','instruction'}
        assert 'Office A' in answer['spoken_answer'] and 'in full' in answer['instruction']
        assert answer['location']=='Office A' and p.writes==1 and not connection.updates
        with g.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(lookup,)).fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM appointment_drafts WHERE call_id=?',(lookup,)).fetchone()[0]==0
        speech(m,lookup,'callee','Actually, my name is Jordan Else.',9000)
        assert await g.own(lookup,dict(first_name='Casey',last_name='Example'))=={'status':'needs_owner'}
        assert p.writes==1
    client.portal.call(exercise)
