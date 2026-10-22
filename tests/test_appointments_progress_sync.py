from datetime import timedelta
from dataclasses import replace
from uuid import uuid4
import pytest
from tests.test_calls import service
from tests.test_appointments_gateway import prepare,agree,speech
from tests.appointment_fixtures import setup,NOW,SLOT
from app.appointments.contracts import CalendarEvent,EventIdentity,ChangePage,ProviderFault
from app.appointments.sync import IndexSync
from app.appointments.registry import Registry,appointment
from app.appointments.gateway import readback_text
from scripts.appointment_registry import preview

@pytest.fixture
def anyio_backend():return 'asyncio'

def test_native_deadline_progress_and_early_conflict_are_not_terminal(service,tmp_path):
    client,m,p,gateway,cid,old=prepare(service,tmp_path)
    native=m.db.create_supervisor_request(cid,'Synthetic appointment request',(NOW+timedelta(seconds=17)).isoformat())
    p.events['busy']=CalendarEvent(EventIdentity(p.identity,'private-busy'),'r',SLOT,owner_label='NEVER SAY THIS')
    d=gateway.store.get('appointment_drafts','draft_id',old['draft_id']);value=appointment(d)
    args={'request_id':native['request_id'],'first_name':value.first_name,'last_name':value.last_name,
        'purpose':value.purpose,'start':SLOT.start.isoformat(),'end':SLOT.end.isoformat(),'time_zone':SLOT.zone,'arrangement':value.location}
    result=client.portal.call(gateway.check,cid,args)
    assert result['decision']=='busy' and result['expires_at']==native['expires_at']
    assert m.db.supervisor_request(native['request_id'])['status']=='pending'
    a=client.portal.call(gateway.progress,native['request_id'],'conflict',result['check_id'])
    b=client.portal.call(gateway.progress,native['request_id'],'conflict',result['check_id'])
    assert a['revision']==b['revision'] and a['request_terminal'] is False
    assert 'NEVER SAY THIS' not in str(m.db.get(cid)['call_updates'])
    assert not m.db.supervisor_request(native['request_id'])['response']


def test_observed_full_delivery_is_distinct_and_durable(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    op=client.portal.call(gateway.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    speech(m,cid,'assistant','Confirmed in October, Office A.',3000)
    assert not gateway.delivery_status(cid,op['operation_id'])['spoken_observed']
    speech(m,cid,'assistant','Confirmed: '+readback_text(value),4000)
    speech(m,cid,'callee','Thank you',5000)
    result=gateway.delivery_status(cid,op['operation_id'])
    assert result['spoken_observed'] and result['caller_receipt_observed'] and not result['injection_accepted']
    with gateway.store.connect() as db:
        row=db.execute("SELECT spoken_at,caller_receipt_at,injection_ack_at FROM booking_outbox WHERE kind='result' ORDER BY operation_revision DESC LIMIT 1").fetchone()
        assert row['spoken_at'] and row['caller_receipt_at'] and row['injection_ack_at'] is None
    assert p.writes==1


@pytest.mark.anyio
async def test_index_backfill_is_dry_first_and_expired_cursor_needs_explicit_resync(tmp_path):
    s,db,p,d,state,now=setup(tmp_path)
    event=CalendarEvent(EventIdentity(p.identity,'existing'),'r1',SLOT,participant_first='Casey',participant_last='Example',disclosure='ordinary')
    p.events['existing']=event
    sync=IndexSync(db,p,lambda:NOW)
    result=await sync.once()
    assert result['eligible']==1 and result['dry_run']
    with db.connect() as c:assert c.execute('SELECT count(*) FROM private_appointments').fetchone()[0]==0
    assert (await sync.once(dry_run=False))['eligible']==1
    async def expired(*a,**k):raise ProviderFault('cursor_expired')
    p.changes=expired
    assert (await sync.once(dry_run=False))['status']=='needs_resync'
    assert (await sync.once(dry_run=False))['status']=='needs_resync'
    assert db.own_appointment('Casey','Example',NOW)['status']=='stale'


def test_schema_preview_never_modifies_original_registry(tmp_path):
    db=Registry(tmp_path/'private.sqlite3');before=db.path.read_bytes()
    result=preview(db.path)
    assert result['dry_run'] and result['schema_version']==1 and result['provider_calls']==0
    assert db.path.read_bytes()==before and not result['source_modified']
