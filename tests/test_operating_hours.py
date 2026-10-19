import asyncio
from datetime import datetime

import pytest
from pydantic import ValidationError

from app.calling.hours import OperatingHours,HoursSettings,HoursWindow
from app.calling.prompts import session_config
from tests.test_calls import service
from tests.test_inbound import incoming,SID
from tests.test_owner_transfer import setup


def policy(**changes):
    return HoursSettings(enabled=True,time_zone='America/New_York',office_name='The office of Dr. Example',
        office_hours=[HoursWindow(days=list(range(7)),start='06:00',end='23:30')],
        owner_reach_hours=[HoursWindow(days=list(range(7)),start='09:00',end='18:00')],
        **changes)


@pytest.mark.parametrize('stamp,office,owner',[
    ('2026-10-19T09:59:00+00:00',False,False), # 05:59 EDT
    ('2026-10-19T10:00:00+00:00',True,False),
    ('2026-10-19T13:00:00+00:00',True,True),
    ('2026-10-19T22:00:00+00:00',True,False),
    ('2026-10-20T03:30:00+00:00',False,False),
    ('2026-11-02T14:00:00+00:00',True,True), # 09:00 EST after DST
    ('2026-10-18T13:00:00+00:00',True,True), # Sunday is included
])
def test_office_and_mobile_hours_boundaries_dst_and_every_day(service,stamp,office,owner):
    client,m,g=service
    m.hours.set(policy())
    m.hours.clock=lambda:datetime.fromisoformat(stamp)
    assert m.hours.state()=={'office_open':office,'owner_reachable':owner}
    assert 'Every day: 6 AM to 11:30 PM'==m.hours.get()['office_hours_spoken']
    persisted=OperatingHours(m.db)
    assert persisted.policy().owner_reach_hours[0].end=='18:00'


@pytest.mark.parametrize('behavior',['closed','messages'])
def test_after_hours_announcement_and_optional_message_only_mode(service,behavior):
    client,m,g=service
    m.settings.inbound_mode='concierge';m.settings.inbound_booking_allowed=True
    m.hours.set(policy(outside_hours=behavior))
    m.hours.clock=lambda:datetime.fromisoformat('2026-10-19T04:00:00+00:00')
    response=incoming(client,m)
    assert 'Dr. Example is currently closed' in response.text
    assert '6 AM to 11:30 PM' in response.text
    if behavior=='closed':
        assert '<Connect' not in response.text and '<Hangup' in response.text
    else:
        assert '<Say>' in response.text and '<Connect><Stream' in response.text
        record=m.voice_record(m.db.by_twilio_sid(SID)['call_id'])
        assert record['inbound_mode']=='messages' and not record['inbound_booking_allowed']
        config=session_config(m.settings,record)
        assert 'already played' in config['input'][0]['content'][0]['text']
        assert {t['name'] for t in config['delegation']['responses']['tools']}=={'save_message','end_call','remember_caller_language'}


def test_mobile_reach_rule_blocks_before_any_paid_transfer(service):
    client,m,g=service
    cid,_=setup(client,m,g)
    m.hours.set(policy())
    m.hours.clock=lambda:datetime.fromisoformat('2026-10-19T23:00:00+00:00')
    with pytest.raises(ValueError,match='mobile reach hours'):
        asyncio.run(m.transfer.request(cid,'Caller asks to speak to the owner'))
    assert not g.redirects and m.transfer.for_call(cid) is None


def test_hours_owner_api_auth_validation_and_disabled_default(service):
    client,m,g=service
    assert not m.hours.get()['enabled'] and m.hours.state()['owner_reachable']
    assert client.get('/hours',headers={'Authorization':''}).status_code==401
    assert client.put('/hours',json=policy().model_dump()).status_code==200
    invalid=policy().model_dump();invalid['time_zone']='invalid/zone'
    assert client.put('/hours',json=invalid).status_code==422
    with pytest.raises(ValidationError): HoursWindow(days=[1,1],start='09:00',end='18:00')
    with pytest.raises(ValidationError): HoursWindow(days=[0],start='23:00',end='06:00')


def test_null_owner_schedule_inherits_and_empty_means_never(service):
    client,m,g=service
    p=policy();p.owner_reach_hours=None;m.hours.set(p)
    m.hours.clock=lambda:datetime.fromisoformat('2026-10-19T12:00:00+00:00')
    assert m.hours.state()['owner_reachable']
    p.owner_reach_hours=[];m.hours.set(p)
    assert not m.hours.state()['owner_reachable']


def test_rocky_can_read_and_replace_hours_through_mcp(service):
    import json
    from app.mcp.server import create_mcp
    client,m,g=service
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    server,_=create_mcp(m)
    async def exercise():
        result=await server.call_tool('set_operating_hours',policy(outside_hours='messages').model_dump())
        assert not result.is_error
        saved=json.loads((await server.call_tool('get_operating_hours',{})).content[0].text)
        assert saved['enabled'] and saved['office_hours'][0]['start']=='06:00'
        assert saved['owner_reach_hours'][0]['start']=='09:00' and saved['outside_hours']=='messages'
    asyncio.run(exercise())
