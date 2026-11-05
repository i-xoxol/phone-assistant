"""Caller ID and portal composer use fake providers and fictional numbers only."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.calling.outbound import OutboundCalls, OutboundDefaults
from app.calling.twilio_client import TwilioGateway
from app.mcp.server import create_mcp
from app.storage.models import CallRequest
from tests.test_calls import service,create
from tests.test_phone_controls import owner_headers
from tests.test_private_portal import portal,sign_in,PRIVATE,PRIVATE_COOKIE


MOBILE='+12025550109'


def brief(**changes):
    return {'phone_number':'+12025550102','objective':'Ask about an existing appointment.',
            'context':'Fictional reference only.','request_id':str(uuid4()),**changes}


def test_twilio_default_preserved_and_pinned(service):
    c,m,g=service
    result=create(c).json()
    assert g.from_numbers==[None] and not g.verification_calls
    assert result['origination']['caller_id']=='twilio'
    assert m.result(result['call_id'])['origination']['from_number']==m.settings.twilio_phone_number
    assert m.outbound.get()['defaults']['caller_id']=='twilio'


@pytest.mark.parametrize('use_env',[False,True])
def test_mobile_selection_reverified_and_actual_from_pinned(service,use_env):
    c,m,g=service
    if use_env:m.settings.outbound_mobile_number=MOBILE
    else:m.settings.owner_transfer_number=MOBILE
    response=c.post('/calls',json=brief(caller_id='mobile'))
    assert response.status_code==201 and len(g.calls)==1
    assert g.from_numbers==[MOBILE] and g.verification_calls==[MOBILE]
    cid=response.json()['call_id'];m.settings.owner_transfer_number='+12025550108'
    assert m.result(cid)['origination']['from_number']==MOBILE


@pytest.mark.parametrize('variant',['missing','unverified','provider_error'])
def test_mobile_failures_never_dial_or_create_a_call(service,variant):
    c,m,g=service;m.settings.owner_transfer_number='' if variant=='missing' else MOBILE
    g.verified=variant!='unverified';g.verification_failure=variant=='provider_error'
    response=c.post('/calls',json=brief(caller_id='mobile'))
    assert response.status_code==503 and not g.calls
    assert 'private-token' not in response.text
    with m.db.connect() as db:assert db.execute('SELECT count(*) FROM calls').fetchone()[0]==0


def test_saved_default_and_portal_brief_survive_restart_without_target(service):
    _,m,g=service;m.settings.owner_transfer_number=MOBILE
    body=OutboundDefaults(caller_id='mobile',objective='Routine status check',context='Public context',
        constraints=['No new charges'],max_duration_minutes=3,ivr_mode=True,revision=1)
    before=asyncio.run(m.outbound.save(body))
    assert 'phone_number' not in before['defaults'] and 'recipient' not in before['defaults']
    assert OutboundCalls(m).get()==before
    result=asyncio.run(m.place_call(CallRequest(phone_number='+12025550102',objective='Actual objective')))
    assert g.from_numbers==[MOBILE]
    # Saving a portal template never silently broadens briefs sent by old MCP/REST clients.
    record=m.db.get(result['call_id']);assert record['context']=='' and record['constraints']==[]
    override=asyncio.run(m.place_call(CallRequest(phone_number='+12025550103',objective='Other',caller_id='twilio')))
    assert override['origination']['caller_id']=='twilio' and g.from_numbers[-1] is None
    with pytest.raises(ValueError,match='changed elsewhere'):asyncio.run(m.outbound.save(body))


def test_request_id_duplicate_and_conflicting_brief_never_redial(service):
    c,m,g=service
    body=brief()
    first=c.post('/calls',json=body).json()
    m.outbound.get()
    second=c.post('/calls',json=body).json()
    assert second['call_id']==first['call_id'] and len(g.calls)==1
    assert c.post('/calls',json={**body,'context':'Changed'}).status_code==503
    assert len(g.calls)==1


def test_uncertain_twilio_failure_replay_does_not_retry(service):
    c,m,g=service;g.failure=True;body=brief()
    first=c.post('/calls',json=body).json();g.failure=False
    second=c.post('/calls',json=body).json()
    assert first['status']==second['status']=='failed'
    assert first['call_id']==second['call_id'] and not g.calls


def test_concurrent_mobile_submission_has_one_create(service):
    _,m,g=service;m.settings.owner_transfer_number=MOBILE
    body=CallRequest(**brief(caller_id='mobile'))
    async def verify(number):await asyncio.sleep(.01);return True
    g.verified_caller_id=verify
    async def run():return await asyncio.gather(m.place_call(body),m.place_call(body))
    rows=asyncio.run(run())
    assert rows[0]['call_id']==rows[1]['call_id'] and len(g.calls)==1


@pytest.mark.parametrize('bad',['+12025550107','arbitrary','owner',''])
def test_only_fixed_caller_id_choices_are_accepted(service,bad):
    c,_,g=service
    assert c.post('/calls',json=brief(caller_id=bad)).status_code==422
    assert not g.calls


def test_portal_placement_context_submission_recovery_and_auth(service):
    c,m,g=service;h=owner_headers(c);m.settings.owner_transfer_number=MOBILE
    body=brief(caller_id='mobile',constraints=['Do not change anything.'],ivr_mode=True,max_duration_minutes=2)
    assert c.post('/live/outbound/calls',json=body).status_code==401
    for headers in ({'Cookie':h['Cookie']},{**h,'Origin':'https://other.test'},{**h,'X-CSRF-Token':'wrong'}):
        assert c.post('/live/outbound/calls',headers=headers,json=body).status_code==403
    result=c.post('/live/outbound/calls',headers=h,json=body)
    assert result.status_code==201 and len(g.calls)==1
    cid=result.json()['call_id'];record=m.db.get(cid)
    assert record['context']==body['context'] and record['constraints']==body['constraints']
    assert record['ivr_mode'] and record['max_duration_minutes']==2
    recovered=c.get('/live/outbound/requests/'+body['request_id'],headers=h).json()
    assert recovered['call_id']==cid and recovered['origination']['from_number']==MOBILE
    assert c.post('/live/outbound/calls',headers=h,json=body).json()['call_id']==cid and len(g.calls)==1
    assert c.post('/live/outbound/calls',headers=h,json=brief(request_id=None)).status_code==422
    assert c.post('/live/outbound/calls',headers=h,json=brief(phone_number='invalid')).status_code==422
    assert c.get('/live/outbound/requests/'+str(uuid4()),headers=h).status_code==404
    c.cookies.clear();c.headers.pop('Authorization')
    assert c.get('/live/outbound/settings').status_code==401


def test_portal_defaults_require_auth_and_do_not_place_calls(service):
    c,m,g=service;h=owner_headers(c)
    result=c.get('/live/outbound/settings',headers=h).json();assert result['defaults']['revision']==1
    data={**result['defaults'],'context':'A reusable fictional brief.'}
    assert c.put('/live/outbound/settings',json=data).status_code==401
    assert c.put('/live/outbound/settings',headers=h,json=data).status_code==200
    assert c.put('/live/outbound/settings',headers=h,json=data).status_code==409
    assert not g.calls


def test_twilio_verified_lookup_and_from_without_changing_transfer_source(service,monkeypatch):
    _,m,_=service;captures=[];lookups=[]
    def lookup(**kwargs):lookups.append(kwargs);return [SimpleNamespace(phone_number=MOBILE)]
    def capture(**kwargs):captures.append(kwargs);return SimpleNamespace(sid='CA_TEST')
    g=TwilioGateway(m.settings)
    monkeypatch.setattr(g,'client',lambda:SimpleNamespace(calls=SimpleNamespace(create=capture),
        outgoing_caller_ids=SimpleNamespace(list=lookup)))
    async def run():
        assert await g.verified_caller_id(MOBILE)
        await g.place('fictional-id','+12025550102',2,from_number=MOBILE)
        await g.ring_owner({'owner_number':MOBILE,'transfer_id':'fictional-transfer'},120)
    asyncio.run(run())
    assert lookups==[{'phone_number':MOBILE,'limit':5}]
    assert captures[0]['from_']==MOBILE and captures[0]['time_limit']==120 and captures[0]['record'] is False
    assert captures[1]['from_']==m.settings.twilio_phone_number


def test_mcp_supports_mobile_id_and_idempotency(service):
    _,m,g=service;m.settings.owner_transfer_number=MOBILE
    server,_=create_mcp(m)
    async def run():
        tools=await server.list_tools();schema=next(t for t in tools if t.name=='place_call').input_schema
        assert {'caller_id','request_id'}<=set(schema['properties'])
        body=brief(caller_id='mobile')
        a=await server.call_tool('place_call',body);b=await server.call_tool('place_call',body)
        assert not a.is_error and not b.is_error
    asyncio.run(run())
    assert len(g.calls)==1 and g.from_numbers==[MOBILE]


def test_private_overlay_can_place_call_with_existing_session_and_csrf(portal):
    _,private,m,auth=portal
    assert sign_in(private).status_code==303
    token=private.cookies.get(PRIVATE_COOKIE)
    h={'Origin':PRIVATE,'X-CSRF-Token':auth.session(token,origin=PRIVATE)['csrf']}
    assert private.get('/live/outbound/settings').status_code==200
    assert private.post('/live/outbound/calls',headers=h,json=brief()).status_code==201
    assert private.post('/live/outbound/calls',headers={**h,'Origin':'https://example.test'},json=brief()).status_code==403
