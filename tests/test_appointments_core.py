from datetime import datetime,timedelta,timezone
from dataclasses import replace
import pytest
from tests.appointment_fixtures import setup,NOW,SLOT,FakeProvider
from app.appointments.contracts import *
from app.appointments.policy import BookingPolicy

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_fifteen_minutes_and_registry(tmp_path):
    s,db,p,d,state,now=setup(tmp_path)
    check=await s.check('call-1',d['draft_id'])
    assert check['decision']=='available' and not check['reservation']
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable-key')
    assert result['state']=='confirmed'
    assert (await s.own_appointment('Casey','Example'))['status']=='match'
    assert (await s.status('call-1',result['operation_id']))['state']=='confirmed'
    assert p.writes==1

@pytest.mark.anyio
@pytest.mark.parametrize('failure',['before','after'])
async def test_uncertain_write_never_repeats(tmp_path,failure):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id']);p.fail=failure
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable-key')
    assert result['state']==('confirmed' if failure=='after' else 'uncertain')
    await s.book('call-1',d['draft_id'],check['check_id'],'yes','another-key')
    assert p.writes==1

@pytest.mark.anyio
@pytest.mark.parametrize('change',['hangup','expired','barge-in','withdrawal'])
async def test_no_start_after_authority_changes(tmp_path,change):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    if change=='hangup':state['active']=False
    if change=='expired':now[0]+=timedelta(seconds=91)
    if change=='barge-in':state['revision']='turn-2'
    if change=='withdrawal':state['allowed']=False
    with pytest.raises(ValueError):await s.book('call-1',d['draft_id'],check['check_id'],'yes','key')
    assert p.writes==0

@pytest.mark.anyio
async def test_change_while_rechecking(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    p.read_hook=lambda:state.update(revision='new-turn')
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','key')
    assert result['state']=='needs_owner' and p.writes==0

@pytest.mark.parametrize('local,fold',[('2026-03-08T02:30:00',None),('2026-11-01T01:30:00',None)])
def test_dst_requires_resolution(local,fold):
    with pytest.raises(ValueError):local_instant(datetime.fromisoformat(local),'America/New_York',fold)

def test_dst_explicit_fold():
    local=datetime(2026,11,1,1,30)
    assert local_instant(local,'America/New_York',0)!=local_instant(local,'America/New_York',1)

@pytest.mark.anyio
async def test_missing_exporter_window_is_not_busy(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);p.complete=False
    assert (await s.check('call-1',d['draft_id']))['decision']=='unknown'

@pytest.mark.anyio
async def test_busy_before_complete_coverage(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);p.complete=False
    p.events['busy']=CalendarEvent(EventIdentity(p.identity,'busy'),'v1',SLOT)
    check=await s.check('call-1',d['draft_id'])
    assert check['decision']=='busy' and 'busy' not in check.get('private_title','')

@pytest.mark.anyio
async def test_no_identity_or_global_context_leak(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    with pytest.raises(ValueError):await s.book('different-call',d['draft_id'],check['check_id'],'yes','key')
    with pytest.raises(ValueError):await s.book('call-1',d['draft_id'],check['check_id'],'no','key')
    assert p.writes==0

@pytest.mark.anyio
async def test_unsupported_guarantees_fail_closed(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);check=await s.check('call-1',d['draft_id'])
    p.capabilities=replace(p.capabilities,suppress_invitations=False)
    assert (await s.book('call-1',d['draft_id'],check['check_id'],'yes','key'))['state']=='needs_owner'
    assert p.writes==0
