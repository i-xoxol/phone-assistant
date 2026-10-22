"""Synthetic MCP booking recovery; no real calls or calendar writes."""
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from app.appointments.gateway import readback_text
from app.appointments.registry import appointment
from app.mcp.server import create_mcp
from tests.appointment_fixtures import NOW
from tests.test_calls import service
from tests.test_appointments_gateway import prepare,speech,agree


def mcp_booking(service,tmp_path,monkeypatch):
    client,m,p,g,cid,r=prepare(service,tmp_path)
    m.settings.appointments_enabled=True;m.settings.oauth_database_path=tmp_path/'oauth.sqlite3'
    mcp,_=create_mcp(m)
    monkeypatch.setattr('app.appointments.mcp.get_access_token',lambda:SimpleNamespace(scopes=['phone.calls','phone.appointments.book']))
    args={'call_id':cid,'request':{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'stable'}}
    async def book():
        result=await mcp.call_tool('book_appointment',args)
        value=result.model_dump(by_alias=True)
        assert not value.get('isError')
        return value.get('structuredContent') or json.loads(value['content'][0]['text'])
    return client,m,p,g,cid,r,args,book


def counts(m,g,cid):
    with g.store.connect() as db:
        operations=db.execute('select count(*) from booking_operations').fetchone()[0]
    return operations,len(m.db.get(cid)['call_updates'])


@pytest.mark.parametrize('rejected_readback',['late_fragment','paraphrase','wrong_time'])
def test_mcp_repairs_prewrite_consent_rejection_then_books_once(service,tmp_path,monkeypatch,rejected_readback):
    client,m,p,g,cid,r,args,book=mcp_booking(service,tmp_path,monkeypatch)
    value=appointment(g.store.get('appointment_drafts','draft_id',r['draft_id']))
    exact='Casey Example, '+readback_text(value)
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    if rejected_readback=='late_fragment':
        # Captions from an earlier recap arrive after the queued readback cursor.
        speech(m,cid,'assistant','26th at ten. ',1000)
        speech(m,cid,'assistant',exact,2300)
    else:
        text='Tomorrow at ten, correct?' if rejected_readback=='paraphrase' else exact.replace('10:00 AM','11:10 AM')
        speech(m,cid,'assistant',text,2300)
    speech(m,cid,'callee','Yes, it is correct.',4000)
    speech(m,cid,'assistant','I am waiting for the appointment check to finish.',5000)
    before=counts(m,g,cid)
    result=client.portal.call(book)
    assert result['state']=='needs_consent' and result['readback_queued']
    assert result['calendar_write_attempted'] is False and p.writes==0
    assert counts(m,g,cid)==(0,before[1]+1)
    # A repair invalidates the old readback/answer. Acknowledgment alone cannot authorize it.
    d=g.store.get('appointment_drafts','draft_id',r['draft_id'])
    assert not g.consent(d,None).affirmative
    with g.store.connect() as db:
        rb=dict(db.execute('select * from appointment_readbacks where draft_id=?',(r['draft_id'],)).fetchone())
    assert rb['after_transcript_id']>0
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE update_id=?",(rb['update_id'],))
    assert not g.consent(d,None).readback_observed
    speech(m,cid,'assistant',exact,6000)
    assert not g.consent(d,None).affirmative
    speech(m,cid,'callee','Sounds good.',7000)
    # A fresh provider check remains necessary if listening outlasts the check TTL.
    now=NOW+timedelta(seconds=35);g.clock=lambda:now;g.service.clock=g.clock;p.clock=g.clock
    result=client.portal.call(book)
    assert result['state']=='confirmed' and p.writes==1
    again=client.portal.call(book)
    assert again['operation_id']==result['operation_id'] and p.writes==1
    assert counts(m,g,cid)[0]==1
    with g.store.connect() as db:assert db.execute('select count(*) from appointment_checks').fetchone()[0]==2
    assert not g.delivery_status(cid,result['operation_id'])['spoken_observed']
    speech(m,cid,'assistant','Confirmed '+readback_text(value).removesuffix(' Is that correct?'),8000)
    speech(m,cid,'callee','Thank you.',9000)
    delivery=g.delivery_status(cid,result['operation_id'])
    assert delivery['spoken_observed'] and delivery['caller_receipt_observed']
    assert m.db.get(cid)['supervisor_requests']==[]


@pytest.mark.parametrize('answer,pending',[
    ('No, do not book it.',False),('Actually, tomorrow instead.',False),
    ('Maybe.',False),('Thank you.',False),('Yes.',True)])
def test_mcp_recovery_waits_for_current_unconditional_answer(service,tmp_path,monkeypatch,answer,pending):
    client,m,p,g,cid,r,args,book=mcp_booking(service,tmp_path,monkeypatch)
    speech(m,cid,'assistant','Is that correct?',1000);speech(m,cid,'callee',answer,2000)
    if pending:m.caller_activity[cid]={'epoch':1,'last_speech_ms':3000}
    before=counts(m,g,cid);result=client.portal.call(book)
    assert result['state']=='needs_consent' and not result['readback_queued']
    assert result['calendar_write_attempted'] is False and p.writes==0
    assert counts(m,g,cid)==before


@pytest.mark.parametrize('guard',['scope','expired','hangup','check_binding','call_binding'])
def test_mcp_recovery_does_not_catch_other_authority_or_binding_errors(service,tmp_path,monkeypatch,guard):
    client,m,p,g,cid,r,args,book=mcp_booking(service,tmp_path,monkeypatch)
    speech(m,cid,'callee','Yes.',2000)
    if guard=='scope':monkeypatch.setattr('app.appointments.mcp.get_access_token',lambda:SimpleNamespace(scopes=['phone.calls']))
    if guard=='expired':g.service.clock=lambda:NOW+timedelta(seconds=46)
    if guard=='hangup':m.transition(cid,'completed')
    if guard=='check_binding':
        with g.store.connect() as db:db.execute('update appointment_checks set draft_revision=2')
    if guard=='call_binding':args['call_id']=str(uuid4())
    before=counts(m,g,cid)
    with pytest.raises(Exception,match='book_appointment'):client.portal.call(book)
    assert counts(m,g,cid)==before and p.writes==0


def test_mcp_recovery_never_replays_uncertain_write(service,tmp_path,monkeypatch):
    client,m,p,g,cid,r,args,book=mcp_booking(service,tmp_path,monkeypatch)
    agree(m,g,cid,r);p.fail='before'
    first=client.portal.call(book)
    assert first['state']=='uncertain' and p.writes==1
    before=counts(m,g,cid)
    speech(m,cid,'callee','No, wait.',3000)
    again=client.portal.call(book)
    assert again['operation_id']==first['operation_id'] and again['state']=='uncertain'
    assert counts(m,g,cid)==before and p.writes==1
