import asyncio
import json
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.calling.context import AvailabilityUpdate,ExpectedCallUpdate,ProfileUpdate,ConciergeContext
from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from app.mcp.server import create_mcp
from tests.test_calls import service
from tests.test_inbound import incoming,SID
from tests.test_live import FakeConnection

UTC=timezone.utc


def availability(**changes):
    stamp=datetime.now(UTC)
    return AvailabilityUpdate(**{'slots':[{'start':stamp+timedelta(days=1),'end':stamp+timedelta(days=1,hours=1)}],
        'time_zone':'America/New_York','observed_at':stamp,'expires_at':stamp+timedelta(minutes=10),
        'source':'Synthetic test calendar',**changes})


def test_owner_context_storage_and_caller_scoping(service):
    client,manager,_=service
    manager.settings.inbound_mode='concierge'
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    profile=ProfileUpdate(role='Personal concierge',context='For friends, ask Rocky about later hours.')
    manager.context.set_profile(profile)
    manager.context.set_availability(availability())
    expiry=datetime.now(UTC)+timedelta(hours=1)
    expected=ExpectedCallUpdate(title='Expected test caller',context='Approved phone-safe note.',phone_number='+12025550101',expires_at=expiry)
    manager.context.expect(expected)
    manager.context.expect(expected)
    other=ExpectedCallUpdate(title='Different caller',context='Do not show this to current caller.',phone_number='+12025550102',expires_at=expiry)
    manager.context.expect(other)
    record=manager.voice_record(cid)
    text=json.dumps(record['incoming_context'])
    assert 'Approved phone-safe note.' in text and 'Do not show this' not in text
    assert len(ConciergeContext(manager.db).snapshot()['expected_calls'])==2
    config=session_config(manager.settings,record)
    assert 'For friends' in config['instructions'] and 'get_call_context' in config['instructions']
    assert 'No standing booking authorization' in config['instructions']
    assert manager.context.for_call({'direction':'outbound'})=={}
    manager.context.retire(str(expected.context_id))
    manager.context.retire(str(expected.context_id))
    assert manager.context.for_call(record)['expected_calls']==[]


def test_expired_context_withheld_and_old_calendar_snapshots_rejected(service):
    _,manager,_=service
    newer=availability()
    manager.context.set_availability(newer)
    old=availability(observed_at=newer.observed_at-timedelta(seconds=5),expires_at=newer.expires_at-timedelta(seconds=5))
    with pytest.raises(ValueError,match='Older availability'): manager.context.set_availability(old)
    assert manager.context.snapshot()['availability']['fresh']
    with manager.db.connect() as conn:
        conn.execute('UPDATE concierge_availability SET expires_at=?',( (datetime.now(UTC)-timedelta(seconds=1)).isoformat(),))
    result=manager.context.snapshot()['availability']
    assert result['fresh'] is False and result['slots']==[]


@pytest.mark.parametrize('changes',[
    {'time_zone':'Not/AZone'},
    {'observed_at':datetime.now(UTC)+timedelta(hours=1)},
    {'expires_at':datetime.now(UTC)+timedelta(hours=2)},
    {'expires_at':datetime.now(UTC)-timedelta(seconds=1)},
    {'slots':[{'start':'2030-01-01T10:00:00','end':'2030-01-01T11:00:00'}]},
])
def test_bad_freshness_zone_and_naive_dates_rejected(changes):
    with pytest.raises(ValidationError): availability(**changes)


def test_overlapping_windows_rejected():
    data=availability()
    with pytest.raises(ValidationError): availability(slots=[data.slots[0],data.slots[0]])


def test_readonly_voice_context_and_live_update_notification(service):
    client,manager,_=service
    manager.settings.inbound_mode='concierge'
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    manager.transition(cid,'in_progress')
    async def exercise():
        bridge=MediaBridge(None,FakeConnection(),manager,cid,'MZ_LOCAL')
        manager.attach_bridge(cid,bridge)
        manager.context.set_profile(ProfileUpdate(role='Concierge',context='Phone-safe test fact.'))
        for n in range(25):
            result=await bridge.tools.operation({'name':'get_call_context','call_id':str(n),'arguments':'{}'})
            assert result['profile']['context']=='Phone-safe test fact.'
            assert result['caller']['number']=='+12025550101'
            assert result['caller']['identity_verified'] is False
        assert manager.db.get(cid)['voice_tool_operations']==[]
        await manager.context_changed()
        updates=manager.db.get(cid)['call_updates']
        assert len(updates)==1 and 'get_call_context' in updates[0]['content']
        assert updates[0]['status']=='sent'
    asyncio.run(exercise())


def test_rest_and_mcp_context_updates_are_owner_only(service):
    client,manager,_=service
    body={'role':'Personal concierge','context':'Friends may ask Rocky about evenings.'}
    assert client.put('/concierge/context',json=body,headers={'Authorization':''}).status_code==401
    assert client.put('/concierge/context',json=body).status_code==200
    assert client.put('/concierge/availability',json=availability().model_dump(mode='json')).status_code==200
    manager.settings.oauth_database_path=manager.db.path.parent/'oauth.db'
    mcp,_=create_mcp(manager)
    async def exercise():
        def data(result):
            assert not result.is_error
            return json.loads(result.content[0].text)
        assert data(await mcp.call_tool('get_inbound_context',{}))['profile']['role']=='Personal concierge'
        assert data(await mcp.call_tool('set_inbound_context',{'role':'Concierge','context':'Updated.'}))['profile']['context']=='Updated.'
        expected={'title':'Anticipated test','context':'Discuss the approved time only.',
            'expires_at':(datetime.now(UTC)+timedelta(hours=2)).isoformat(),'context_id':str(uuid4())}
        value=data(await mcp.call_tool('expect_incoming_call',expected))
        assert value['status']=='active'
        assert data(await mcp.call_tool('retire_expected_call',{'context_id':value['context_id']}))['status']=='retired'
    asyncio.run(exercise())
