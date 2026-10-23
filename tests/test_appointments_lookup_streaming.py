"""Read-only lookup must settle current identity instead of failing on benign speech."""
import asyncio,json,time
import pytest
from app.appointments.evidence import caller_identified
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,agree,speech


def identity_turns(reply):
    return [dict(speaker=s,text=t,timestamp=i,end_timestamp=i+.2) for i,(s,t) in enumerate([
        ('callee','My name is Casey Example.'),('assistant','Would you like me to reach the owner or take a message?'),('callee',reply)])]


@pytest.mark.parametrize('reply',['No, no, no. No, check Casey Example.','Please recheck Casey Example.','Look up Casey Example.'])
def test_rechecking_the_same_proven_name_is_lookup_only(reply):
    assert caller_identified(identity_turns(reply),'Casey','Example',for_lookup=True)
    if reply.startswith('No,'):assert not caller_identified(identity_turns(reply),'Casey','Example')


@pytest.mark.parametrize('reply',['No, check Casey','No, check Casey Exampleton','No, check Jordan Else',
    'No, check Casey Example for my friend',"No, my name is Jordan Else.","Wait, I'm"])
def test_a_recheck_cannot_supply_a_different_partial_or_third_party_identity(reply):
    assert not caller_identified(identity_turns(reply),'Casey','Example',for_lookup=True)


def test_recheck_does_not_establish_identity_or_answer_a_new_name_question():
    rows=identity_turns('No, check Casey Example.')
    assert not caller_identified(rows[1:],'Casey','Example',for_lookup=True)
    rows[1]['text']='Your full name?'
    assert not caller_identified(rows,'Casey','Example',for_lookup=True)


def test_harmless_caller_followup_during_verified_read_does_not_reject_the_same_identity(service,tmp_path,monkeypatch):
    client,m,p,g,cid,result=prepare(service,tmp_path)
    speech(m,cid,'callee','My name is Casey Example.',1000)
    async def lookup(*args,**kwargs):
        speech(m,cid,'callee','Can you check if I booked?',2000)
        return {'status':'match','location':'Office A'}
    monkeypatch.setattr(g.service,'own_appointment',lookup)
    result=client.portal.call(g.own,cid,dict(first_name='Casey',last_name='Example'))
    assert result['status']=='match' and result['location']=='Office A' and p.writes==0


@pytest.mark.parametrize('during',['captions_pending','followup_during_read','same_name_recheck','new_name',
    'new_name_during_read','pending_timeout','hangup','hangup_during_read'])
def test_native_lookup_waits_for_caption_evidence_and_never_creates_another_booking(service,tmp_path,during):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    booked=client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    assert booked['state']=='confirmed' and p.writes==1
    booked_call=cid;m.transition(cid,'completed')
    cid=m.db.create_inbound('CA_LOOKUP_STREAM','+12025550111',15,10,'concierge',True)
    m.transition(cid,'in_progress')
    m.settings.appointments_enabled=True;g.lookup_settle_seconds=.01;g.lookup_wait_seconds=.03 if during=='pending_timeout' else .3
    # Read-only lookup in this live call; no new check, draft, operation or write.
    speech(m,cid,'callee','My name is Casey Example.',3000)
    if during=='same_name_recheck':
        speech(m,cid,'assistant','Would you like me to reach the owner or take a message?',4000)
        speech(m,cid,'callee','No, no, no. No, check Casey Example.',5000)
    async def exercise():
        con=UpdateConnection();b=MediaBridge(None,con,m,cid,'MZ_LOOKUP_STREAM');m.attach_bridge(cid,b)
        reads=[]
        in_read=during in ('followup_during_read','new_name_during_read','hangup_during_read')
        if not in_read and during!='same_name_recheck':
            m.caller_activity[cid]={'epoch':1,'last_speech_ms':5500};b.last_input_speech_at=time.monotonic()
        async def finish():
            await asyncio.sleep(.02)
            if during in ('hangup','hangup_during_read'):m.transition(cid,'completed')
            elif during!='pending_timeout':
                speech(m,cid,'callee','Actually, my name is Jordan Else.' if during in ('new_name','new_name_during_read') else 'Can you check if I booked?',5000)
        def read_hook():
            reads.append(1)
            if in_read and len(reads)==1:
                m.caller_activity[cid]={'epoch':2,'last_speech_ms':5500};b.last_input_speech_at=time.monotonic()
                asyncio.create_task(finish())
        p.read_hook=read_hook
        caption_task=asyncio.create_task(finish()) if not in_read and during!='same_name_recheck' else None
        try:result=await b.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Casey',last_name='Example'))})
        finally:
            if caption_task:await caption_task
        if during in ('captions_pending','followup_during_read','same_name_recheck'):
            assert result['status']=='match' and result['location']=='Office A'
            if during=='followup_during_read':assert len(reads)>=2
        elif during=='pending_timeout':
            assert result['status']=='pending' and result['reason']=='caller_captions_pending'
            assert not {'start','location','end'}&set(result)
        else:
            assert result['status']=='needs_owner' and not {'start','location','end'}&set(result)
        assert p.writes==1 and not con.updates
        with g.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(booked_call,)).fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
    client.portal.call(exercise)
