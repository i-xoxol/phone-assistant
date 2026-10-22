import asyncio
from types import SimpleNamespace
from uuid import uuid4
import pytest
from tests.test_calls import service
from tests.test_appointments_gateway import prepare,speech
from app.mcp.server import create_mcp
from app.appointments.schemas import SlotRequest,BookRequest
from app.config import Settings


def test_versioned_mcp_tools_register_without_exposing_private_coach_search(service,tmp_path,monkeypatch):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    m.settings.appointments_enabled=True;m.settings.oauth_database_path=tmp_path/'oauth.sqlite3'
    mcp,oauth=create_mcp(m)
    async def exercise():
        tools={t.name:t for t in await mcp.list_tools()}
        for name in ('check_appointment_slot','book_appointment','get_booking_status','get_my_appointment'):
            assert tools[name].model_dump(by_alias=True)['_meta']['schemaVersion']=='1.0'
        assert 'get_owner_schedule' not in tools
        schema=tools['book_appointment'].model_dump(by_alias=True)['inputSchema']
        assert 'call_id' in schema['properties'] and 'request' in schema['properties']
        assert 'authorized' not in str(schema) and 'calendar_id' not in str(schema)
        monkeypatch.setattr('app.appointments.mcp.get_access_token',lambda:SimpleNamespace(scopes=['phone.calls']))
        # FastMCP reports tool errors rather than converting an absent scope into permission.
        with pytest.raises(Exception,match='get_my_appointment'):
            await mcp.call_tool('get_my_appointment',{'call_id':cid,'first_name':'Casey','last_name':'Example'})
        monkeypatch.setattr('app.appointments.mcp.get_access_token',lambda:SimpleNamespace(scopes=['phone.calls','phone.appointments.read']))
        result=await mcp.call_tool('get_my_appointment',{'call_id':cid,'first_name':'Casey','last_name':'Example'})
        assert 'needs_owner' in str(result)
    asyncio.run(exercise())


def test_extra_model_authority_and_missing_runtime_boundary_rejected():
    with pytest.raises(ValueError):BookRequest(draft_id=uuid4(),check_id=uuid4(),idempotency_key='one',authorized=True)
    with pytest.raises(ValueError):Settings(_env_file=None,owner_calendar_coaching_enabled=True)
    with pytest.raises(ValueError):Settings(_env_file=None,appointments_writes_enabled=True)
