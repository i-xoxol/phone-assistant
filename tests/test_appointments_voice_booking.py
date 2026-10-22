"""Synthetic live-turn regressions; no calls or real calendar writes."""
import json
from datetime import timedelta
import pytest
from app.appointments.evidence import complete_readback
from app.appointments.gateway import readback_text
from app.appointments.registry import appointment
from app.calling.live_session import MediaBridge
from tests.appointment_fixtures import NOW,setup
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,speech


@pytest.mark.parametrize('date',[
    'October twenty-sixth twenty twenty-six', 'October twenty sixth two thousand twenty six',
    'October 26th two thousand and twenty-six', 'October twenty-six 2026'])
def test_exact_spoken_date_preserves_all_fields(tmp_path,date):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    text=('Casey Example, '+readback_text(value)).replace('October 26 2026',date)
    assert complete_readback(text,value)
    assert not complete_readback(text.replace('10:00 AM','11:10 AM'),value)
    assert not complete_readback(text.replace('15 minutes','one hundred fifteen minutes'),value)


@pytest.mark.parametrize('date',[
    'October twenty-third twenty twenty-six', 'October twenty-sixth twenty twenty-seven',
    'October twenty-sixth twenty twenty-six or twenty-seven',
    'October one hundred twenty-sixth twenty twenty-six'])
def test_different_or_ambiguous_spoken_date_cannot_match(tmp_path,date):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    assert not complete_readback(('Casey Example, '+readback_text(value)).replace('October 26 2026',date),value)


@pytest.mark.parametrize('yes',["Yes, it is correct.","Yes, that's right.","Yes, it’s right."])
def test_clear_current_affirmative_after_spoken_date_books_once(service,tmp_path,yes):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    text=('Casey Example, '+readback_text(value)).replace('October 26 2026','October twenty-sixth twenty twenty-six').replace('America/New_York','Eastern Time')
    for i,part in enumerate([text[:25],text[25:60],text[60:]]):speech(m,cid,'assistant',part,1000+500*i)
    speech(m,cid,'callee',yes,3000)
    args={'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'}
    assert client.portal.call(gateway.book,cid,args)['state']=='confirmed'
    assert client.portal.call(gateway.book,cid,args)['state']=='confirmed' and p.writes==1


def test_voice_recovers_rejected_readback_without_supervisor_or_write_retry(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);m.settings.appointments_enabled=True
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    # A natural paraphrase is not proof of the exact complete payload. Repair it.
    speech(m,cid,'assistant','Just confirming your appointment tomorrow at ten. Is that correct?',1000)
    speech(m,cid,'callee','Yes, it is correct.',2000)
    item={'name':'book_appointment','call_id':'tool-book','arguments':json.dumps({
        'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})}
    async def exercise():
        connection=UpdateConnection();bridge=MediaBridge(None,connection,m,cid,'MZ_CONSENT')
        m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation(item)
        assert result['state']=='needs_consent' and result['readback_queued']
        assert result['calendar_write_attempted'] is False and p.writes==0
        with gateway.store.connect() as db:
            assert db.execute('select count(*) from booking_operations').fetchone()[0]==0
            readback=db.execute('select * from appointment_readbacks where draft_id=?',(r['draft_id'],)).fetchone()
        assert readback['after_transcript_id']>0
        channel,command=connection.updates[-1]
        assert channel=='instructions' and 'without a preface' in command['content']
        assert 'Eastern Time' in command['content'] and 'America/New_York' not in command['content']
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
        # The earlier yes cannot authorize the repaired readback.
        assert not gateway.consent(gateway.store.get('appointment_drafts','draft_id',r['draft_id']),None).affirmative
        speech(m,cid,'assistant','Casey Example, '+readback_text(value),4000)
        speech(m,cid,'callee','Yes, it is correct.',5000)
        result=await bridge.tools.operation(item)
        assert result['state']=='confirmed' and p.writes==1
        assert 'source_identity' not in result
        assert (await bridge.tools.operation(item))['operation_id']==result['operation_id'] and p.writes==1
        # Confirmed booking queues an explicit spoken result; acknowledgement alone is not speech.
        channel,command=connection.updates[-1]
        assert channel=='instructions' and 'Confirmed ' in command['content']
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
        assert gateway.delivery_status(cid,result['operation_id'])['spoken_observed'] is False
        speech(m,cid,'assistant','Confirmed '+readback_text(value).removesuffix(' Is that correct?'),6000)
        speech(m,cid,'callee','Thank you.',7000)
        delivery=gateway.delivery_status(cid,result['operation_id'])
        assert delivery['spoken_observed'] and delivery['caller_receipt_observed']
        assert m.db.get(cid)['supervisor_requests']==[]
    client.portal.call(exercise)


@pytest.mark.parametrize('answer,pending',[
    ('No, do not book it.',False),('Actually, make it Friday instead.',False),('Yes.',True)])
def test_consent_repair_respects_refusal_corrections_and_unfinished_audio(service,tmp_path,answer,pending):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);m.settings.appointments_enabled=True
    speech(m,cid,'assistant','Is that correct?',1000);speech(m,cid,'callee',answer,2000)
    if pending:m.caller_activity[cid]={'epoch':1,'last_speech_ms':3000}
    async def exercise():
        connection=UpdateConnection();bridge=MediaBridge(None,connection,m,cid,'MZ_REFUSAL');m.attach_bridge(cid,bridge)
        await bridge.send_pending_updates();before=len(connection.updates)
        result=await bridge.tools.operation({'name':'book_appointment','call_id':'tool-book','arguments':json.dumps({
            'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})})
        assert result['state']=='needs_consent' and not result['readback_queued']
        assert len(connection.updates)==before and p.writes==0
    client.portal.call(exercise)


@pytest.mark.anyio
async def test_expired_check_refreshes_live_data_after_consent_without_extending_deadline(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    now[0]+=timedelta(seconds=35)
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable')
    assert result['state']=='confirmed' and p.writes==1
    with db.connect() as c:checks=[dict(r) for r in c.execute('select * from appointment_checks order by checked_at')]
    assert len(checks)==2 and checks[0]['expires_at']==check['expires_at']
    assert checks[1]['checked_at']==now[0].isoformat()
    assert checks[1]['expires_at']<=(NOW+timedelta(seconds=90)).isoformat()


@pytest.mark.anyio
@pytest.mark.parametrize('change',['busy','incomplete','hangup','withdrawal','deadline','caller'])
async def test_expired_check_refresh_preserves_all_write_guards(tmp_path,change):
    from app.appointments.contracts import CalendarEvent,EventIdentity
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    now[0]+=timedelta(seconds=35)
    def changed():
        if change=='busy':p.events['busy']=CalendarEvent(EventIdentity(p.identity,'busy'),'v',appointment(d).interval)
        if change=='incomplete':p.complete=False
        if change=='hangup':state['active']=False
        if change=='withdrawal':state['allowed']=False
        if change=='deadline':now[0]=NOW+timedelta(seconds=91)
        if change=='caller':state['revision']='changed'
    p.read_hook=changed
    try:
        result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable')
        assert result['state'] in ('conflict','needs_owner')
    except ValueError:pass
    assert p.writes==0
    with db.connect() as c:assert c.execute('select count(*) from booking_operations where write_attempted=1').fetchone()[0]==0
