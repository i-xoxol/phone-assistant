"""An exact lookup miss needs spelling repair, never guessed identity or disclosure."""
import json
import pytest
from app.appointments.evidence import caller_identified
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,agree,speech


def spelling_turns(first='C A S E Y',last='E X A M P L E'):
    pairs=[('callee','My name is Kasey Example.'),('assistant','Could you spell your first name?'),('callee',first),
        ('assistant','And could you spell your last name?'),('callee',last)]
    return [dict(speaker=s,text=t,timestamp=i,end_timestamp=i+.3) for i,(s,t) in enumerate(pairs)]


@pytest.mark.parametrize('first,last',[
    ('C A S E Y','E X A M P L E'),('C-A-S-E-Y','E-X-A-M-P-L-E'),('C. A. S. E. Y.','E. X. A. M. P. L. E.'),
    ("It's C, A, S, E, Y.","It's E, X, A, M, P, L, E."),
    ('C A S E Y',"It's Example."),
    ('C as in Charlie, A as in Apple, S as in Sam, E as in Echo, Y as in Yellow','E X A M P L E'),
])
def test_explicit_separate_spelling_establishes_the_corrected_exact_full_name(first,last):
    rows=spelling_turns(first,last)
    assert caller_identified(rows,'Casey','Example',for_lookup=True)
    assert not caller_identified(rows,'Casey','Example')  # Booking evidence rules are unchanged.


@pytest.mark.parametrize('first,last',[
    ('K A S E Y','E X A M P L E'),('C A S','E X A M P L E'),('C A S E Y','E X A M P L'),
    ('C A S E Y for my friend','E X A M P L E'),('C A S E Y but tomorrow','E X A M P L E'),
    ('C A S E Y','not E X A M P L E'),('C as in Charlie and Jordan Else','E X A M P L E'),
])
def test_spelling_does_not_drop_wrong_missing_third_party_or_conditional_content(first,last):
    assert not caller_identified(spelling_turns(first,last),'Casey','Example',for_lookup=True)


def test_partial_spelling_or_one_unseparated_full_name_cannot_supply_missing_components():
    assert not caller_identified(spelling_turns()[:3],'Casey','Example',for_lookup=True)
    rows=spelling_turns()[:1]+[dict(speaker='assistant',text='Could you spell your full name?',timestamp=1,end_timestamp=1.3),
        dict(speaker='callee',text='C A S E Y E X A M P L E',timestamp=2,end_timestamp=2.3)]
    assert not caller_identified(rows,'Casey','Example',for_lookup=True)


def test_spelling_a_third_party_name_does_not_identify_the_caller():
    rows=spelling_turns();rows[1]['text']="Could you spell your friend's first name?";rows[3]['text']="Your friend's last name?"
    assert not caller_identified(rows,'Casey','Example',for_lookup=True)


def test_native_exact_miss_requests_one_spelling_repair_then_matches_without_a_new_booking(service,tmp_path):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    assert client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))['state']=='confirmed'
    m.transition(cid,'completed');booked_call=cid
    cid=m.db.create_inbound('CA_LOOKUP_SPELL','+12025550111',15,10,'concierge',True);m.transition(cid,'in_progress')
    m.settings.appointments_enabled=True
    async def exercise():
        con=UpdateConnection();b=MediaBridge(None,con,m,cid,'MZ_SPELL');m.attach_bridge(cid,b)
        speech(m,cid,'callee','My name is Kasey Example.',1000)
        result=await b.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Kasey',last_name='Example'))})
        assert result['status']=='needs_name_spelling' and result['reason']=='no_exact_name_match'
        assert result['missing_fields']==['first_name','last_name'] and 'spell your first name' in result['instruction']
        assert not {'start','end','location','candidates'}&set(result)
        for i,row in enumerate(spelling_turns()[1:]):speech(m,cid,row['speaker'],row['text'],2000+i*1000)
        fixed=await b.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Casey',last_name='Example'))})
        assert fixed['status']=='match' and fixed['location']=='Office A' and 'purpose' not in fixed
        assert p.writes==1 and not con.updates
        with g.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(booked_call,)).fetchone()[0]==1
        # A second exact miss after spelling offers one review, not another spelling loop.
        speech(m,cid,'assistant','Could you spell your first name?',7000)
        speech(m,cid,'callee','K A S E Y',8000)
        speech(m,cid,'assistant','Your last name?',9000);speech(m,cid,'callee','Example',10000)
        missing=await b.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Kasey',last_name='Example'))})
        assert missing['status']=='not_found' and missing['reason']=='exact_name_not_found_after_spelling'
        assert 'owner review' in missing['instruction'] and p.writes==1
    client.portal.call(exercise)
