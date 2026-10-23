"""A real two-model failure: assistant acknowledgment split the caller's full name."""
import pytest
import json
from app.appointments.evidence import caller_identified
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,agree,speech


def split_name(ack='Mm-hmm.',tail=' Example.'):
    return [dict(speaker=s,text=t,timestamp=i,end_timestamp=i+.4) for i,(s,t) in enumerate([
        ('callee','No, no. I called earlier. My name is Casey'),('assistant',ack),('callee',tail)])]


@pytest.mark.parametrize('ack',['Mm-hmm.','Mhm.','Hmm.','Uh-huh.'])
def test_neutral_backchannel_does_not_split_a_complete_lookup_name(ack):
    rows=split_name(ack)
    assert caller_identified(rows,'Casey','Example',for_lookup=True)
    assert not caller_identified(rows,'Casey','Example')  # Booking evidence keeps its existing rules.


@pytest.mark.parametrize('ack,tail',[
    ('Mm-hmm, who are you asking for?',' Example.'),('Mm-hmm?',' Example.'),
    ('Mm-hmm. Is that for your friend?',' Example.'),('Mm-hmm.',' Actually, my name is Jordan Else.'),
    ('Mm-hmm.',' Example for my friend.'),('Mm-hmm.',' Examp.'),
])
def test_backchannels_cannot_hide_questions_changes_third_parties_or_partial_names(ack,tail):
    assert not caller_identified(split_name(ack,tail),'Casey','Example',for_lookup=True)


def test_native_lookup_verifies_the_existing_event_after_a_backchannel_without_booking(service,tmp_path):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    m.transition(cid,'completed');cid=m.db.create_inbound('CA_BACKCHANNEL','+12025550101',3,10,'concierge',True)
    m.transition(cid,'in_progress');m.settings.appointments_enabled=True
    for i,row in enumerate(split_name()):speech(m,cid,row['speaker'],row['text'],i*1000)
    async def check():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_BACKCHANNEL');m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Casey',last_name='Example'))})
        assert result['status']=='match' and result['location']=='Office A' and 'purpose' not in result
        assert p.writes==1
        with g.store.connect() as db:assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
    client.portal.call(check)
