"""An exact lookup miss needs spelling repair, never guessed identity or disclosure."""
import json
from datetime import timedelta
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
        assert result['status']=='needs_name_search' and 'Please hold' in result['instruction']
        assert 'search_appointment_name' in result['instruction']
        assert not {'start','end','location','candidates'}&set(result)
        speech(m,cid,'assistant','Please hold while I check the upcoming appointments for a different spelling.',1600)
        result=await b.tools.operation({'name':'search_appointment_name','arguments':json.dumps(dict(first_name='Kasey',last_name='Example'))})
        assert result['status']=='needs_name_spelling' and result['reason']=='no_exact_name_match'
        assert result['missing_fields']==['first_name','last_name'] and 'spell your first name' in result['instruction']
        assert 'Casey' not in json.dumps(result) and not {'start','end','location','candidates'}&set(result)
        for i,row in enumerate(spelling_turns()[1:]):speech(m,cid,row['speaker'],row['text'],2000+i*1000)
        # Even if the model retains its original ASR spelling, the server can use
        # a close hint only after the current caller independently spells the exact name.
        fixed=await b.tools.operation({'name':'search_appointment_name','arguments':json.dumps(dict(first_name='Kasey',last_name='Example'))})
        assert fixed['status']=='match' and fixed['location']=='Office A' and 'purpose' not in fixed
        assert p.writes==1 and not con.updates
        with g.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(booked_call,)).fetchone()[0]==1
        # A second exact miss after spelling offers one review, not another spelling loop.
        speech(m,cid,'assistant','Could you spell your first name?',7000)
        speech(m,cid,'callee','K A S E Y',8000)
        speech(m,cid,'assistant','Your last name?',9000);speech(m,cid,'callee','Example',10000)
        missing=await b.tools.operation({'name':'search_appointment_name','arguments':json.dumps(dict(first_name='Kasey',last_name='Example'))})
        assert missing['status']=='not_found' and missing['reason']=='exact_name_not_found_after_spelling'
        assert 'owner review' in missing['instruction'] and p.writes==1
    client.portal.call(exercise)


@pytest.mark.parametrize('prefix',['Uh-hai, uh, ','Um, hi, ','Hey, uh, '])
def test_lookup_greeting_hesitations_do_not_hide_a_complete_self_introduction(prefix):
    rows=[dict(speaker='callee',text=prefix+'my name is Casey Example. I called earlier. I want to verify my appointment.',timestamp=0,end_timestamp=4)]
    assert caller_identified(rows,'Casey','Example',for_lookup=True)
    rows += [dict(speaker='assistant',text='Would you like owner review?',timestamp=5,end_timestamp=6),
        dict(speaker='callee',text="Can you check, uh, it's for Casey Example",timestamp=7,end_timestamp=9)]
    assert caller_identified(rows,'Casey','Example',for_lookup=True)
    assert not caller_identified(rows,'Casey','Example')


@pytest.mark.parametrize('text',["Can you check, uh, it's for Casey Example",
    'Uh-hai, uh, my friend is Casey Example.',"Uh-hai, uh, my name is Casey Example for my friend."])
def test_hesitations_or_same_name_recheck_cannot_create_third_party_identity(text):
    assert not caller_identified([dict(speaker='callee',text=text,timestamp=0,end_timestamp=1)],'Casey','Example',for_lookup=True)


@pytest.mark.parametrize('exclude',['cancelled','needs_owner','sensitive','outside_horizon','ended','unrelated'])
def test_internal_spelling_review_reads_only_supported_upcoming_name_hints(service,tmp_path,exclude):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    assert g.store.close_appointment_names('Kasey','Example',g.clock())==[dict(first_name='Casey',last_name='Example')]
    with g.store.connect() as db:
        if exclude in ('cancelled','needs_owner'):db.execute('UPDATE private_appointments SET state=?',(exclude,))
        elif exclude=='sensitive':db.execute("UPDATE private_appointments SET disclosure='sensitive'")
        elif exclude=='outside_horizon':db.execute('UPDATE private_appointments SET start=?,end=?',
            ((g.clock()+timedelta(days=100)).isoformat(),(g.clock()+timedelta(days=101)).isoformat()))
        elif exclude=='ended':db.execute('UPDATE private_appointments SET end=?',((g.clock()-timedelta(seconds=1)).isoformat(),))
        else:db.execute("UPDATE private_appointments SET first_name='Jordan',last_name='Else'")
    assert g.store.close_appointment_names('Kasey','Example',g.clock())==[]
    assert p.writes==1


def test_internal_spelling_review_fails_closed_instead_of_using_a_truncated_name_list(service,tmp_path):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    with g.store.connect() as db:
        row=dict(db.execute('SELECT * FROM private_appointments').fetchone());columns=list(row)
        for i in range(501):
            new={**row,'appointment_id':'candidate-'+str(i),'operation_id':None,'event_id':'event-'+str(i),
                'first_name':'Person'+str(i),'first_key':'person'+str(i)}
            db.execute('INSERT INTO private_appointments('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(new[k] for k in columns))
    assert g.store.close_appointment_names('Kasey','Example',g.clock()) is None
    assert p.writes==1


@pytest.mark.parametrize('during',['changed_name','hangup','stale_source'])
def test_close_hint_still_requires_fresh_source_current_identity_and_live_call(service,tmp_path,during):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    m.transition(cid,'completed');cid=m.db.create_inbound('CA_REVIEW_GUARD','+12025550111',15,10,'concierge',True);m.transition(cid,'in_progress')
    m.settings.appointments_enabled=True
    for i,row in enumerate(spelling_turns()):speech(m,cid,row['speaker'],row['text'],1000+i*1000)
    async def exercise():
        b=MediaBridge(None,UpdateConnection(),m,cid,'MZ_REVIEW_GUARD');m.attach_bridge(cid,b)
        def interrupt():
            if during=='hangup':m.transition(cid,'completed')
            elif during=='changed_name':speech(m,cid,'callee','Actually, my name is Jordan Else.',9000)
        p.read_hook=interrupt
        if during=='stale_source':p.events.clear()
        result=await b.tools.operation({'name':'search_appointment_name','arguments':json.dumps(dict(first_name='Kasey',last_name='Example'))})
        assert result['status'] in ('needs_owner','stale')
        assert not {'start','end','location','candidates'}&set(result) and p.writes==1
        with g.store.connect() as db:assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
    client.portal.call(exercise)
