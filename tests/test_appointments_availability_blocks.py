import asyncio
import threading
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.appointment_fixtures import setup,SLOT
from app.appointments.contracts import CalendarEvent,EventIdentity,Interval


def test_fourteen_day_read_returns_only_free_office_blocks_and_never_writes(tmp_path):
    svc,store,p,d,state,now=setup(tmp_path)
    p.events['busy']=CalendarEvent(EventIdentity(p.identity,'busy'),'r',Interval(SLOT.start+timedelta(minutes=30),SLOT.start+timedelta(minutes=45),SLOT.zone))
    r=asyncio.run(svc.availability_blocks('call-1','request-1'))
    assert r['decision']=='available' and r['reservation'] is False and p.writes==0
    assert [(b['start'],b['end']) for b in r['blocks']]==[
        (SLOT.start.isoformat(),(SLOT.start+timedelta(minutes=30)).isoformat()),
        ((SLOT.start+timedelta(minutes=45)).isoformat(),(SLOT.start+timedelta(hours=2)).isoformat())]
    assert 'busy' not in str(r) and 'first_name' not in str(r)


@pytest.mark.parametrize('kind',['incomplete','expired','future','unknown','no_office','hangup'])
def test_bad_or_unauthorized_coverage_never_returns_free_blocks(tmp_path,kind):
    svc,store,p,d,state,now=setup(tmp_path);original=p.read_availability
    async def read(interval):
        c=await original(interval)
        if kind=='incomplete':return replace(c,complete=False)
        if kind=='expired':return replace(c,valid_until=now[0])
        if kind=='future':return replace(c,checked_at=now[0]+timedelta(seconds=1))
        if kind=='unknown':return replace(c,events=(replace(p.office,state='unknown'),))
        if kind=='no_office':return replace(c,events=())
        state['active']=False;return c
    p.read_availability=read
    if kind=='hangup':
        with pytest.raises(ValueError):asyncio.run(svc.availability_blocks('call-1','request-1'))
    else:assert not asyncio.run(svc.availability_blocks('call-1','request-1'))['blocks']
    assert p.writes==0


def test_touching_office_occurrences_are_not_advertised_as_one_unbookable_block(tmp_path):
    svc,store,p,d,state,now=setup(tmp_path)
    p.office=replace(p.office,interval=Interval(SLOT.start,SLOT.start+timedelta(hours=1),SLOT.zone))
    p.events['second']=replace(p.office,identity=EventIdentity(p.identity,'office2'),
        interval=Interval(SLOT.start+timedelta(hours=1),SLOT.start+timedelta(hours=2),SLOT.zone))
    r=asyncio.run(svc.availability_blocks('call-1','request-1'))
    assert len(r['blocks'])==2


def test_calendar_block_scan_keeps_the_live_audio_event_loop_responsive(tmp_path):
    svc,store,p,d,state,now=setup(tmp_path);policy=svc.policy;entered=threading.Event();release=threading.Event()
    class SlowPolicy:
        first=True
        def __getattr__(self,key):return getattr(policy,key)
        def office_hours(self,interval):
            if self.first:
                self.first=False;entered.set()
                assert release.wait(2),'Calendar scan blocked the audio event loop'
            return policy.office_hours(interval)
    svc.policy=SlowPolicy()
    async def exercise():
        job=asyncio.create_task(svc.availability_blocks('call-1','request-1'))
        assert await asyncio.to_thread(entered.wait,2)
        release.set()
        result=await job
        assert result['decision']=='available'
    asyncio.run(exercise())
