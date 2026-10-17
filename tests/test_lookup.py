import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.calling.context import utcnow
from app.calling.knowledge import CallKnowledge,ContactUpdate,FactUpdate
from app.calling.lookup import Selection,cli_arguments
from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from app.mcp.server import create_mcp
from tests.test_calls import service
from tests.test_inbound import incoming,SID
from tests.test_live import FakeConnection


def fact(**kwargs):
    return FactUpdate(**{'topic':'Demo project','text':'The demo keyword is lavender.',
        'source':'Owner verified test','expires_at':utcnow()+timedelta(days=1),
        'approved_for_callers':True,**kwargs})


def setup_call(client,manager):
    manager.settings.inbound_mode='concierge'
    manager.settings.subscription_lookup_enabled=True
    incoming(client,manager)
    cid=manager.db.by_twilio_sid(SID)['call_id']
    manager.transition(cid,'in_progress')
    bridge=MediaBridge(None,FakeConnection(),manager,cid,'MZ_TEST')
    bridge.connection.session.thinking=SimpleNamespace(append=bridge.connection.instructions)
    manager.attach_bridge(cid,bridge)
    return cid,bridge


def test_curated_contacts_persist_but_numbers_never_reach_worker(service):
    _,m,_=service
    contact=ContactUpdate(name='Casey Example',phone_number='+12025550109')
    m.knowledge.contact(contact)
    note=fact(contact_id=contact.contact_id,topic='Delivery',text='Please leave demo parcels at reception.')
    m.knowledge.fact(note)
    k=CallKnowledge(m.db)
    assert not k.search('parcels')['facts']
    result=k.search('Casey parcels')
    assert result['facts'][0]['text']==note.text
    assert '+12025550109' not in json.dumps(result)
    assert k.search('parcels',caller_number=contact.phone_number)['facts']
    assert k.search(owner=True)['contacts'][0]['phone_number']==contact.phone_number
    k.retire(str(note.fact_id))
    assert not k.search('Casey parcels')['facts']


@pytest.mark.parametrize('changes',[{'approved_for_callers':False},{'expires_at':utcnow()-timedelta(seconds=1)},
    {'expires_at':utcnow()+timedelta(days=100)},{'text':'я'*126},{'text':'   '}])
def test_invalid_or_unapproved_facts_rejected(changes):
    with pytest.raises(ValidationError): fact(**changes)


def test_fact_retirement_expiry_and_changed_data_withheld(service):
    _,m,_=service
    note=fact()
    m.knowledge.fact(note)
    snapshot=m.knowledge.search('demo')['facts']
    m.knowledge.fact(fact(fact_id=note.fact_id,text='The replacement keyword is cedar.'))
    assert m.knowledge.still_current(snapshot)==[]
    with m.db.connect() as db:
        db.execute('UPDATE concierge_facts SET expires_at=?',((utcnow()-timedelta(seconds=1)).isoformat(),))
    assert m.knowledge.search('demo')['facts']==[]


def test_owner_only_rest_and_mcp_directory(service):
    client,m,_=service
    data=fact().model_dump(mode='json')
    assert client.put('/concierge/facts',json=data,headers={'Authorization':''}).status_code==401
    assert client.put('/concierge/facts',json=data).status_code==200
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    mcp,_=create_mcp(m)
    async def exercise():
        result=await mcp.call_tool('search_call_knowledge',{'query':'demo'})
        assert json.loads(result.content[0].text)['facts'][0]['text']==data['text']
        await mcp.call_tool('retire_call_fact',{'fact_id':data['fact_id']})
    asyncio.run(exercise())
    assert client.get('/concierge/knowledge?query=demo').json()['facts']==[]


def test_cli_forces_chatgpt_readonly_no_inherited_tools(tmp_path):
    args=cli_arguments('codex',tmp_path,tmp_path/'schema.json')
    assert args[args.index('--model')+1]=='gpt-6-luna'
    assert 'forced_login_method="chatgpt"' in args and '--ignore-user-config' in args
    assert args[args.index('--sandbox')+1]=='read-only'
    for key in ('shell_tool','apps','plugins','computer_use','multi_agent','hooks','code_mode_host'):
        assert args[args.index(key)-1]=='--disable'
    assert 'web_search="disabled"' in args


def test_quick_lookup_selected_facts_injected_async_and_duplicate_collapsed(service):
    client,m,_=service
    note=fact()
    m.knowledge.fact(note)
    async def exercise():
        cid,bridge=setup_call(client,m)
        gate=asyncio.Event()
        async def runner(question,candidates,timeout):
            assert '+12025550101' not in json.dumps(candidates)
            await gate.wait()
            return Selection(selected_ids=[str(note.fact_id)],needs_supervisor=False)
        m.lookup.runner=runner
        first=await bridge.tools.operation({'name':'request_quick_lookup','call_id':'lookup-1',
            'arguments':json.dumps({'question':'What is the demo keyword?'})})
        assert first['status']=='pending' and not m.db.get(cid)['call_updates']
        second=await m.lookup.request(cid,'repeat demo question')
        assert second['request_id']==first['request_id']
        gate.set()
        await asyncio.gather(*list(m.lookup.tasks.values()))
        result=m.result(cid)
        assert result['lookup_requests'][0]['status']=='answered'
        assert result['lookup_requests'][0]['delivery_status']=='sent'
        assert note.text in result['call_updates'][0]['content']
        assert result['call_updates'][0]['mode']=='say'
        assert result['voice_tool_operations'][0]['name']=='request_quick_lookup'
        config=session_config(m.settings,m.voice_record(cid))
        assert 'request_quick_lookup' in {t['name'] for t in config['delegation']['responses']['tools']}
    asyncio.run(exercise())


@pytest.mark.parametrize('scenario',['unknown_id','timeout','needs_supervisor','retired','ended','replaced_bridge'])
def test_failed_stale_or_ended_lookup_cannot_invent_or_deliver_facts(service,scenario):
    client,m,_=service
    note=fact()
    m.knowledge.fact(note)
    async def exercise():
        cid,bridge=setup_call(client,m)
        async def runner(*_):
            if scenario=='unknown_id': return Selection(selected_ids=[str(uuid4())],needs_supervisor=False)
            if scenario=='timeout': raise TimeoutError('private text must never be saved')
            if scenario=='retired': m.knowledge.retire(str(note.fact_id))
            if scenario=='ended': m.transition(cid,'completed')
            if scenario=='replaced_bridge': m.bridges[cid]=object()
            return Selection(selected_ids=[str(note.fact_id)],needs_supervisor=scenario=='needs_supervisor')
        m.lookup.runner=runner
        await m.lookup.request(cid,'demo keyword')
        await asyncio.gather(*list(m.lookup.tasks.values()))
        result=m.result(cid)
        assert note.text not in json.dumps(result['call_updates'])
        assert 'private text' not in json.dumps(result['lookup_requests'])
        if scenario in ('ended','replaced_bridge'):
            assert not result['call_updates'] and result['lookup_requests'][0]['status']=='call_ended'
    asyncio.run(exercise())


def test_lookup_disabled_missing_facts_and_rate_limits(service):
    client,m,_=service
    async def exercise():
        cid,bridge=setup_call(client,m)
        assert (await m.lookup.request(cid,'demo'))['status']=='no_matching_facts'
        m.knowledge.fact(fact())
        m.settings.subscription_lookup_enabled=False
        assert (await m.lookup.request(cid,'demo'))['status']=='unavailable'
        m.settings.subscription_lookup_enabled=True
        with m.db.connect() as db:
            for _ in range(6):
                db.execute('INSERT INTO concierge_lookups(request_id,call_id,question,status,created_at) VALUES (?,?,?,?,?)',
                    (str(uuid4()),cid,'demo','answered',utcnow().isoformat()))
        assert (await m.lookup.request(cid,'demo'))['status']=='unavailable'
    asyncio.run(exercise())


def test_luna_recommendation_cannot_dial_the_owner_directly(service):
    client,m,_=service
    m.knowledge.fact(fact())
    async def exercise():
        cid,_=setup_call(client,m)
        m.settings.owner_transfer_enabled=True
        async def recommend(*_): return Selection(selected_ids=[],needs_supervisor=True,recommend_owner=True)
        m.lookup.runner=recommend
        await m.lookup.request(cid,'demo')
        await asyncio.gather(*list(m.lookup.tasks.values()))
        assert m.transfer.for_call(cid) is None
        updates=m.db.get(cid)['call_updates']
        assert any('after agreement use reach_owner' in row['content'] for row in updates)
    asyncio.run(exercise())
