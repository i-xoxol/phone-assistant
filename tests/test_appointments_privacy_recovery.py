from dataclasses import replace
from datetime import timedelta
import pytest
from tests.appointment_fixtures import setup,NOW,SLOT
from app.appointments.contracts import *
from app.appointments.registry import Registry,appointment
from app.appointments.disclosure import PrivateNotices

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_registry_failure_retains_one_event_then_repairs(tmp_path,monkeypatch):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    original=db.confirmed
    def fail(*a):raise OSError('synthetic registry outage')
    monkeypatch.setattr(db,'confirmed',fail)
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','key')
    assert result['state']=='uncertain' and p.writes==1 and len(p.events)==1
    monkeypatch.setattr(db,'confirmed',original)
    s.store=Registry(db.path)  # Process restart, same durable operation identity.
    repaired=await s.status('call-1',result['operation_id'])
    assert repaired['state']=='confirmed' and p.writes==1

@pytest.mark.anyio
async def test_external_editor_race_never_deletes_or_overwrites(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id']);create=p.create
    async def race(identity,value,*,authorize):
        await create(identity,value,authorize=authorize)
        p.events['external']=CalendarEvent(EventIdentity(p.identity,'external'),'new',SLOT)
    p.create=race
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','key')
    assert result['state']=='needs_owner' and result['step']=='external_race'
    assert len(p.events)==2 and p.writes==1

@pytest.mark.anyio
async def test_changed_payload_invalidates_old_check(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    changed=replace(appointment(d),location='Remote arrangement')
    newer=s.prepare('call-1','request-1',changed)
    assert newer['revision']==2
    with pytest.raises(ValueError):await s.book('call-1',d['draft_id'],check['check_id'],'yes','key')
    assert p.writes==0

@pytest.mark.anyio
async def test_duplicate_full_names_no_candidate_disclosure(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    await s.book('call-1',d['draft_id'],check['check_id'],'yes','key')
    event=next(iter(p.events.values()));second=replace(event,identity=EventIdentity(p.identity,'second'),interval=Interval(SLOT.start+timedelta(days=3),SLOT.end+timedelta(days=3),SLOT.zone))
    db.ingest(ChangePage((second,),'c1',True),p.identity,NOW,dry_run=False)
    assert await s.own_appointment('Casey','Example')=={'status':'ambiguous'}
    assert await s.own_appointment('Unknown','Person')=={'status':'not_found'}
    assert await s.own_appointment('Casey','Example',contradictory=True)=={'status':'needs_owner'}

def test_verified_aliases_merge_and_absence_does_not_cancel(tmp_path):
    s,db,p,d,state,now=setup(tmp_path)
    source=EventIdentity(CalendarIdentity('source','account','calendar'),'opaque-source','occurrence-1')
    event=CalendarEvent(EventIdentity(p.identity,'mirror-1','occurrence-1'),'etag-1',SLOT,
        aliases=(source,),alias_verified=True,participant_first='Casey',participant_last='Example',disclosure='ordinary')
    dry=db.ingest(ChangePage((event,),'cursor-1',True),p.identity,NOW)
    assert dry['eligible']==1
    assert db.own_appointment('Casey','Example',NOW)['status']=='not_found'
    db.ingest(ChangePage((event,),'cursor-1',True),p.identity,NOW,dry_run=False)
    moved=replace(event,revision='etag-2',identity=EventIdentity(p.identity,'mirror-2','occurrence-1'))
    db.ingest(ChangePage((moved,),'cursor-2',True),p.identity,NOW,dry_run=False,expected_cursor='cursor-1')
    with db.connect() as conn:assert conn.execute('SELECT count(*) FROM private_appointments').fetchone()[0]==1
    db.ingest(ChangePage((),'cursor-3',True),p.identity,NOW,dry_run=False,expected_cursor='cursor-2')
    assert db.own_appointment('Casey','Example',NOW)['status']=='match'
    db.tombstone(source)
    assert db.own_appointment('Casey','Example',NOW)['status']=='not_found'

@pytest.mark.parametrize('fault',['ambiguous','contradictory','unknown','not_curated','expired'])
def test_bounded_family_notices(tmp_path,fault):
    s,db,p,d,state,now=setup(tmp_path);notices=PrivateNotices(db)
    with db.connect() as conn:conn.execute('INSERT INTO appointment_relationships VALUES(?,?,1)',('contact-1','wife'))
    notices.set('contact','contact-1','whereabouts','teaching',NOW,NOW+timedelta(hours=1),source='owner',now=NOW)
    contacts=[{'contact_id':'contact-1','phone_number':'+12025550122'}]
    if fault=='ambiguous':contacts*=2
    if fault=='unknown':contacts=[]
    if fault=='not_curated':contacts=[{'contact_id':'unapproved','phone_number':'+12025550122'}]
    result=notices.for_family(contacts,number='+12025550122',ambiguous=False,contradictory=fault=='contradictory',now=NOW+timedelta(hours=2) if fault=='expired' else NOW)
    if fault=='expired':assert result['notices']==[]
    else:assert result=={'status':'needs_owner'}

def test_no_sensitive_or_invented_status(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);notices=PrivateNotices(db)
    with pytest.raises(ValueError):notices.set('contact','a','busy_reason','medical treatment',NOW,NOW+timedelta(hours=1),source='owner',now=NOW)
    with pytest.raises(ValueError):notices.set('contact','a','whereabouts','teaching',NOW,NOW+timedelta(hours=1),source='calendar_title',now=NOW)
    with pytest.raises(ValueError):notices.set('appointment','a','delay_minutes','10',NOW-timedelta(days=2),NOW-timedelta(days=1),source='owner',now=NOW)

def test_ambiguous_verified_aliases_invalidate_both_records_without_candidates(tmp_path):
    s,db,p,d,state,now=setup(tmp_path)
    first=CalendarEvent(EventIdentity(p.identity,'one'),'r1',SLOT,participant_first='Casey',participant_last='Example',disclosure='ordinary')
    second=replace(first,identity=EventIdentity(p.identity,'two'),participant_first='Other')
    db.ingest(ChangePage((first,second),'cursor-1',True),p.identity,NOW,dry_run=False)
    ambiguous=replace(first,identity=EventIdentity(p.identity,'merged'),aliases=(first.identity,second.identity),alias_verified=True)
    result=db.ingest(ChangePage((ambiguous,),'cursor-2',True),p.identity,NOW,dry_run=False,expected_cursor='cursor-1')
    assert result['needs_owner']==1
    assert db.own_appointment('Casey','Example',NOW)=={'status':'stale'}
    assert db.own_appointment('Other','Example',NOW)=={'status':'stale'}

@pytest.mark.anyio
async def test_changed_source_disclosure_never_returns_old_ordinary_details(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','one')
    event=next(iter(p.events.values()))
    p.events[event.identity.event]=replace(event,revision='new-sensitive',disclosure='sensitive')
    assert await s.own_appointment('Casey','Example')=={'status':'stale'}

def test_new_owner_status_supersedes_old_value_for_same_private_audience(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);n=PrivateNotices(db)
    with db.connect() as c:c.execute('INSERT INTO appointment_relationships VALUES(?,?,1)',('contact-1','friend'))
    n.set('contact','contact-1','whereabouts','office',NOW,NOW+timedelta(hours=1),source='owner',now=NOW)
    n.set('contact','contact-1','whereabouts','away',NOW,NOW+timedelta(hours=1),source='owner',now=NOW)
    result=n.for_family([{'contact_id':'contact-1','phone_number':'+12025550111'}],number='+12025550111',ambiguous=False,contradictory=False,now=NOW)
    assert [r['value'] for r in result['notices']]==['away']
