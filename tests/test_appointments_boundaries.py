from dataclasses import replace
from datetime import timedelta,date
import pytest
from app.appointments.contracts import *
from app.appointments.policy import BookingPolicy
from app.appointments.registry import Registry
from tests.appointment_fixtures import setup,NOW,SLOT

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.parametrize('start,duration,expected',[(0,15,True),(105,15,True),(106,15,False),(-1,15,False),(0,121,False)])
def test_whole_office_interval_boundaries(start,duration,expected):
    a=SLOT.start+timedelta(minutes=start);slot=Interval(a,a+timedelta(minutes=duration),SLOT.zone)
    assert BookingPolicy().office_hours(slot)==expected

def test_current_exceptions_buffers_and_busy_inside_office():
    s,db,p,d,state,now=None,None,None,None,None,None
    office=CalendarEvent(EventIdentity(CalendarIdentity('fake','a','b'),'office'),'v',Interval(SLOT.start,SLOT.end+timedelta(hours=1),SLOT.zone),kind='availability')
    busy=CalendarEvent(EventIdentity(office.identity.calendar,'busy'),'v',Interval(SLOT.end+timedelta(minutes=2),SLOT.end+timedelta(minutes=10),SLOT.zone))
    coverage=Coverage(Interval(SLOT.start-timedelta(hours=1),SLOT.end+timedelta(hours=1),SLOT.zone),NOW,NOW+timedelta(minutes=1),True,(office,busy))
    assert BookingPolicy(buffer_after=5).decide(SLOT,coverage,NOW)[0]=='busy'
    assert BookingPolicy(closed_dates=frozenset({date(2026,10,26)})).decide(SLOT,replace(coverage,events=(office,)),NOW)[0]=='outside_hours'
    assert BookingPolicy().decide(SLOT,replace(coverage,events=(office,replace(busy,interval=SLOT))),NOW)[0]=='busy'

def test_past_office_occurrence_is_not_bookable():
    slot=Interval(SLOT.start-timedelta(days=7),SLOT.end-timedelta(days=7),SLOT.zone)
    office=CalendarEvent(EventIdentity(CalendarIdentity('fake','a','b'),'past-office'),'v',slot,kind='availability')
    coverage=Coverage(slot,NOW,NOW+timedelta(minutes=1),True,(office,))
    assert BookingPolicy().decide(slot,coverage,NOW)==('outside_hours','past_interval')

def test_sync_replay_order_series_tombstone_and_expired_cursor(tmp_path):
    s,db,p,d,state,now=setup(tmp_path)
    event=CalendarEvent(EventIdentity(p.identity,'instance:1','opaque-original'),'v1',SLOT,series_id='series:1',
        participant_first='Casey',participant_last='Example',disclosure='ordinary')
    page=ChangePage((event,),'cursor-a',True)
    db.ingest(page,p.identity,NOW,dry_run=False)
    assert db.ingest(page,p.identity,NOW,dry_run=False)['duplicate']
    with pytest.raises(ValueError):db.ingest(ChangePage((),'out-of-order',True),p.identity,NOW,dry_run=False,expected_cursor='wrong')
    db.cursor_expired(p.identity,NOW)
    with pytest.raises(ValueError):db.ingest(ChangePage((event,),'cursor-b',True),p.identity,NOW,dry_run=False)
    db.ingest(ChangePage((event,),'cursor-b',True),p.identity,NOW,dry_run=False,resync=True)
    db.tombstone(EventIdentity(p.identity,'series:1'))
    assert db.own_appointment('Casey','Example',NOW)['status']=='not_found'
    with db.connect() as conn:revision=conn.execute('SELECT revision FROM private_appointments').fetchone()[0]
    db.tombstone(EventIdentity(p.identity,'series:1'))
    with db.connect() as conn:assert conn.execute('SELECT revision FROM private_appointments').fetchone()[0]==revision

def test_notice_cannot_change_audience_or_kind(tmp_path):
    from app.appointments.disclosure import PrivateNotices
    s,db,p,d,state,now=setup(tmp_path);n=PrivateNotices(db)
    nid=n.set('contact','one','whereabouts','office',NOW,NOW+timedelta(hours=1),source='owner',now=NOW)
    with pytest.raises(ValueError):n.set('contact','two','whereabouts','office',NOW,NOW+timedelta(hours=1),source='owner',now=NOW,notice_id=nid)

def test_core_has_no_vendor_imports_or_shapes():
    import ast
    from pathlib import Path
    root=Path(__file__).parents[1]/'app/appointments'
    for filename in ('contracts.py','policy.py','service.py','registry.py'):
        text=(root/filename).read_text();tree=ast.parse(text)
        imports=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        assert not any('google' in n or 'graph' in n for n in imports)
        assert 'extendedProperties' not in text and 'recurringEventId' not in text
