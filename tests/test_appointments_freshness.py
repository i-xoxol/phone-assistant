"""Exercise elapsed calendar I/O instead of freezing both sides of the read."""
import asyncio
from dataclasses import replace
from datetime import timedelta,datetime
from types import SimpleNamespace
import httpx,pytest
from app.appointments.contracts import Coverage,Interval,CalendarEvent,EventIdentity
from app.appointments.coaching import owner_schedule
from app.appointments.google import GoogleCalendarAdapter,MirrorHealth
from app.appointments.service import CallAuthority
from tests.appointment_fixtures import setup,NOW,SLOT
from tests.test_appointments_google import office,health,token


@pytest.fixture
def anyio_backend():return 'asyncio'


@pytest.mark.anyio
async def test_google_check_uses_completion_time_with_advancing_clock(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);completed=[];reads=[]
    def clock():
        now[0]+=timedelta(microseconds=10)
        return now[0]
    async def handle(request):
        reads.append(request.url.host)
        await asyncio.sleep(.01)
        now[0]+=timedelta(milliseconds=150)
        completed.append(now[0])
        return httpx.Response(200,json=health() if request.url.host=='mirror.test' else {'items':[office()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        s.clock=clock
        s.provider=GoogleCalendarAdapter(http,token,replace(p.identity,provider='google'),MirrorHealth(http,'http://mirror.test',clock,
            verified_window=True),clock,office_ids=['trusted-office'])
        result=await s.check('call-1',d['draft_id'])
    assert result['decision']=='available' and result['reason']=='verified_with_mirror_lag'
    checked=datetime.fromisoformat(result['checked_at']);expires=datetime.fromisoformat(result['expires_at'])
    assert checked>=completed[-1]>NOW
    assert checked<expires<=NOW+timedelta(seconds=30,milliseconds=1)
    assert expires<checked+timedelta(seconds=30)  # Provider validity still starts before its I/O.
    assert reads==['www.googleapis.com','mirror.test'] and not result['reservation']
    assert p.writes==0


@pytest.mark.anyio
@pytest.mark.parametrize('fault,reason',[
    ('incomplete','incomplete_coverage'),('mirror','mirror_stale_or_failed'),
    ('interval','insufficient_interval_coverage'),('future','future_coverage'),('expired','expired_coverage')])
async def test_check_rejects_unusable_coverage_with_accurate_reason(tmp_path,fault,reason):
    s,db,p,d,state,now=setup(tmp_path)
    async def read(interval,exclude=None):
        await asyncio.sleep(.001);now[0]+=timedelta(seconds=1)
        c=Coverage(interval,now[0],now[0]+timedelta(seconds=30),True,(p.office,),'verified_with_mirror_lag')
        if fault=='incomplete':c=replace(c,complete=False)
        if fault=='mirror':c=replace(c,complete=False,reason='mirror_stale_or_failed')
        if fault=='interval':c=replace(c,interval=Interval(interval.start+timedelta(seconds=1),interval.end,interval.zone))
        if fault=='future':c=replace(c,checked_at=now[0]+timedelta(seconds=1))
        if fault=='expired':c=replace(c,valid_until=now[0])
        return c
    p.read_interval=read
    result=await s.check('call-1',d['draft_id'])
    assert (result['decision'],result['reason'])==('unknown',reason)
    assert p.writes==0 and not result['reservation']


@pytest.mark.anyio
@pytest.mark.parametrize('change',['hangup','deadline','withdrawal'])
async def test_authority_lost_during_await_leaves_no_check(tmp_path,change):
    s,db,p,d,state,now=setup(tmp_path)
    async def read(interval,exclude=None):
        await asyncio.sleep(.001)
        if change=='hangup':state['active']=False
        if change=='deadline':now[0]+=timedelta(seconds=91)
        if change=='withdrawal':state['allowed']=False
        return Coverage(interval,now[0],now[0]+timedelta(seconds=30),True,(p.office,))
    p.read_interval=read
    with pytest.raises(ValueError,match='current booking authority'):await s.check('call-1',d['draft_id'])
    with db.connect() as conn:assert conn.execute('SELECT count(*) FROM appointment_checks').fetchone()[0]==0
    assert p.writes==0


@pytest.mark.anyio
async def test_changed_live_deadline_caps_check_expiry(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);deadline=[NOW+timedelta(seconds=90)]
    s.authority=lambda c,r:CallAuthority(True,deadline[0],'turn-1',True)
    async def read(interval,exclude=None):
        await asyncio.sleep(.001);now[0]+=timedelta(seconds=1);deadline[0]=NOW+timedelta(seconds=5)
        return Coverage(interval,now[0],now[0]+timedelta(seconds=60),True,(p.office,))
    p.read_interval=read
    result=await s.check('call-1',d['draft_id'])
    assert result['decision']=='available' and datetime.fromisoformat(result['expires_at'])==deadline[0]


@pytest.mark.anyio
async def test_fresh_coverage_does_not_extend_check_ttl(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);s.policy=replace(s.policy,check_ttl_seconds=3)
    p.read_hook=lambda:now.__setitem__(0,now[0]+timedelta(seconds=1))
    result=await s.check('call-1',d['draft_id'])
    assert result['decision']=='available'
    assert datetime.fromisoformat(result['expires_at'])==datetime.fromisoformat(result['checked_at'])+timedelta(seconds=3)


@pytest.mark.anyio
@pytest.mark.parametrize('restriction,expected',[
    ('busy',('busy','overlapping_commitment')),
    ('hours',('outside_hours','academic_office_hours')),
    ('occurrence',('unknown','office_occurrence_not_verified'))])
async def test_advancing_clock_keeps_conflict_hours_and_occurrence_gates(tmp_path,restriction,expected):
    s,db,p,d,state,now=setup(tmp_path)
    async def read(interval,exclude=None):
        await asyncio.sleep(.001);now[0]+=timedelta(seconds=1)
        events=() if restriction=='occurrence' else (p.office,)
        if restriction=='busy':events+=(CalendarEvent(EventIdentity(p.identity,'busy'),'v',interval),)
        return Coverage(interval,now[0],now[0]+timedelta(seconds=30),restriction!='busy',events,'verified_with_mirror_lag')
    if restriction=='hours':s.policy=replace(s.policy,weekly=())
    p.read_interval=read
    result=await s.check('call-1',d['draft_id'])
    assert (result['decision'],result['reason'])==expected and p.writes==0


@pytest.mark.anyio
@pytest.mark.parametrize('fault,reason',[
    ('incomplete','incomplete_coverage'),('interval','insufficient_interval_coverage'),
    ('future','future_coverage'),('expired','expired_coverage'),('healthy',None)])
async def test_owner_agenda_validates_at_completion_with_accurate_reason(tmp_path,fault,reason):
    s,db,p,d,state,now=setup(tmp_path);p.capabilities=replace(p.capabilities,owner_agenda=True)
    async def read(interval):
        await asyncio.sleep(.001);now[0]+=timedelta(seconds=1)
        c=Coverage(interval,now[0],now[0]+timedelta(seconds=30),True,reason='verified_with_mirror_lag')
        if fault=='incomplete':c=replace(c,complete=False)
        if fault=='interval':c=replace(c,interval=SLOT)
        if fault=='future':c=replace(c,checked_at=now[0]+timedelta(seconds=1))
        if fault=='expired':c=replace(c,valid_until=now[0])
        return c
    p.read_agenda=read
    result=await owner_schedule(SimpleNamespace(service=s,clock=lambda:now[0]),{
        'start':SLOT.start,'end':SLOT.end+timedelta(days=1),'time_zone':SLOT.zone,'view':'free_busy'},lambda:True)
    if reason:assert result=={'status':'unknown','reason':reason}
    else:assert result['status']=='current'
