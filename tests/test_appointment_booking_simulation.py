import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace

import pytest
from scripts.simulate_appointment_booking import CappedCalendar,BookingScenario,caller_config,validate_execution,heard_confirmation
from tests.appointment_fixtures import NOW,SLOT,FakeProvider
from app.appointments.contracts import Appointment
from app.appointments.registry import payload_hash
from app.storage.database import Database
from app.config import Settings


def test_dry_run_creates_only_a_private_local_event_and_reconciles_it():
    async def exercise():
        provider=FakeProvider(lambda:NOW);value=Appointment('Casey','Example','course guidance',SLOT,'Office A')
        capped=CappedCalendar(provider,value);identity=provider.intended_identity('dry')
        await capped.create(identity,value,authorize=lambda:None)
        assert provider.writes==0 and capped.creates==1
        assert (await capped.read_event(identity)).booking_details_verified
        assert len((await capped.read_interval(SLOT)).events)==2
        assert len((await capped.read_interval(SLOT,exclude=identity)).events)==1
        with pytest.raises(ValueError):await capped.create(identity,value,authorize=lambda:None)
    asyncio.run(exercise())


@pytest.mark.parametrize('change',['date','time','duration','name','purpose','room'])
def test_execution_cap_rejects_changed_booking_before_provider_dispatch(change):
    async def exercise():
        provider=FakeProvider(lambda:NOW);value=Appointment('Casey','Example','course guidance',SLOT,'Office A')
        capped=CappedCalendar(provider,value,execute=True)
        changed=replace(value,**({'first_name':'Other'} if change=='name' else {'purpose':'Other'} if change=='purpose' else
            {'location':'Office B'} if change=='room' else {'interval':replace(SLOT,
                **({'start':SLOT.start+timedelta(days=1),'end':SLOT.end+timedelta(days=1)} if change=='date' else
                {'start':SLOT.start+timedelta(minutes=1),'end':SLOT.end+timedelta(minutes=1)} if change=='time' else
                {'end':SLOT.end+timedelta(minutes=1)}))}))
        with pytest.raises(ValueError):await capped.create(provider.intended_identity('real'),changed,authorize=lambda:None)
        assert provider.writes==0 and capped.creates==0
    asyncio.run(exercise())


def test_cap_accepts_same_room_format_but_not_a_different_number():
    p=FakeProvider(lambda:NOW);v=Appointment('Casey','Example','course guidance',SLOT,'East Building, West Campus, room E 1066')
    cap=CappedCalendar(p,v)
    assert cap.permits(replace(v,location='In person, East Building, West Campus, room E-1066'))
    assert not cap.permits(replace(v,location='East Building, West Campus, room E 1067'))


def test_real_mode_requires_current_private_exact_owner_grant_and_idle_source(tmp_path):
    s=Settings(_env_file=None,database_path=tmp_path/'calls.sqlite3');db=Database(s.database_path)
    cid=db.create_inbound('LOCAL_SOURCE','+12025550101',3,10,'concierge',True);db.transition(cid,'completed')
    scenario=BookingScenario(source_call_id=cid,appointment=dict(first_name='Casey',last_name='Example',purpose='course guidance',
        start=SLOT.start,end=SLOT.end,time_zone=SLOT.zone,arrangement='Office A'),script=['Book Monday at ten.'])
    p=tmp_path/'grant.json';grant={'action':'one_real_booking_without_telephone','source_call_id':cid,
        'payload_hash':payload_hash(scenario.appointment.appointment()),'max_events':1,
        'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
    p.write_text(json.dumps(grant));p.chmod(0o600)
    assert len(validate_execution(s,scenario,p))==64
    with pytest.raises(ValueError):validate_execution(s,scenario,None)
    p.chmod(0o644)
    with pytest.raises(ValueError):validate_execution(s,scenario,p)
    p.chmod(0o600);grant['payload_hash']='different';p.write_text(json.dumps(grant))
    with pytest.raises(ValueError):validate_execution(s,scenario,p)


def test_caller_is_a_separate_natural_booking_peer_without_tools():
    scenario=BookingScenario(source_call_id='source',appointment=dict(first_name='Casey',last_name='Example',purpose='course guidance',
        start=SLOT.start,end=SLOT.end,time_zone=SLOT.zone,arrangement='Office A'),script=['Book Monday at ten.'])
    config=caller_config(Settings(_env_file=None),scenario,'marin')
    assert config['delegation']=={'type':'client'} and not config['store']
    assert 'That works for me' in config['instructions'] and 'BOOK' in config['instructions']


def test_result_scoring_waits_for_playback_and_independent_positive_caller_audio():
    events=[dict(side='caller',direction='input',start_ms=1000,text='Confirmed.')]
    assert not heard_confirmation(events,{'start_ms':None})
    assert not heard_confirmation(events,{'start_ms':2000})
    assert heard_confirmation(events,{'start_ms':500})
    assert not heard_confirmation([{**events[0],'text':'Not confirmed.'}],{'start_ms':500})
    assert not heard_confirmation([{**events[0],'side':'assistant','direction':'output'}],{'start_ms':500})
