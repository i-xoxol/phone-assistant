"""Deterministic routing, scoped facts, owner approvals, and external-agent controls."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID,uuid4

import pytest
from pydantic import ValidationError

from app.api.personalities import Preview,prompt_preview
from app.calling.context import utcnow
from app.calling.disclosures import DisclosureRequest
from app.calling.knowledge import ContactUpdate,FactUpdate
from app.calling.personalities import PersonalityUpdate
from app.calling.prompts import session_config
from app.calling.rules import Actions,Conditions,RoutingRule,RuleSet,RulePreview,ReviewRequest,ReviewResult
from app.mcp.server import create_mcp
from app.storage.models import CallRequest,CallUpdateRequest
from tests.test_calls import service,create
from tests.test_phone_controls import owner_headers
from tests.test_voice_backends import configure


NUMBER='+12025550102'


def person(m,level='general',score=None):
    p=ContactUpdate(name='Fictional contact',phone_number=NUMBER,information_level=level,routing_score=score)
    m.knowledge.contact(p,True)
    return p


def fact(m,audience='general',contact_id=None,text='A fictional reference fact.'):
    f=FactUpdate(topic='Contact email' if audience=='approval_required' else 'Example topic',text=text,source='Owner',
        expires_at=utcnow()+timedelta(days=1),approved_for_callers=audience!='approval_required',audience=audience,contact_id=contact_id)
    m.knowledge.fact(f)
    return f


def call(m,number=NUMBER):
    cid=m.db.create(CallRequest(phone_number=number,objective='Fictional check'))
    m.db.transition(cid,'in_progress');m.voice_record(cid)
    return cid


def rule(**changes):
    return RoutingRule(**{'name':'Fictional routing','when':Conditions(direction='outbound',phone_numbers=[NUMBER]),
                          'then':Actions(provider='openai'),**changes})


def test_no_rules_default_preserves_contacts_and_role(service):
    _,m,_=service
    assert m.rules.get()['rules']==[]
    p=person(m,'family',80)
    saved=m.knowledge.caller_book()[0]
    assert saved['information_level']=='family' and saved['routing_score']==80
    m.knowledge.contact(ContactUpdate(contact_id=p.contact_id,name=p.name,phone_number=NUMBER,preferred_language='uk'))
    assert m.disclosures.contact(p.contact_id)=={'level':'family','score':80}
    assert m.rules.preview(RulePreview(phone_number=NUMBER,direction='outbound'))['selection']['rule_id'] is None


def test_rule_priority_forwarding_and_missing_metadata(service):
    _,m,_=service;person(m,'friends',72)
    profile=m.personalities.save(PersonalityUpdate(name='Fictional office',greeting='Hello from the office.'))
    first=rule(name='Office forwarding',priority=10,when=Conditions(direction='inbound',forwarded_from='+12025550109'),
               then=Actions(personality_id=profile['personality_id'],greeting='A brief office hello.'))
    second=rule(name='Contact score',priority=20,when=Conditions(contact_group='friends',minimum_score=70),then=Actions(instructions='Be informal.'))
    m.rules.save(RuleSet(revision=0,rules=[second,first]))
    preview=m.rules.preview(RulePreview(phone_number=NUMBER,forwarded_from='+12025550109'))
    assert preview['selection']['name']=='Office forwarding' and preview['personality']['personality_id']==profile['personality_id']
    assert preview['personality']['greeting']=='A brief office hello.'
    assert m.rules.preview(RulePreview(phone_number=NUMBER))['selection']['name']=='Contact score'
    assert m.rules.preview(RulePreview(phone_number='+12025550107'))['selection']['rule_id'] is None
    # Equal presented and forwarding numbers cannot select caller-specific rules.
    assert m.rules.preview(RulePreview(phone_number=NUMBER,forwarded_from=NUMBER))['selection']['rule_id'] is None
    with pytest.raises(ValueError,match='changed elsewhere'):m.rules.save(RuleSet(revision=0,rules=[]))


def test_explicit_call_choices_override_rule_and_existing_calls_keep_snapshot(service):
    _,m,g=service;person(m)
    automatic=m.personalities.save(PersonalityUpdate(name='Office routing'))
    me=m.personalities.save(PersonalityUpdate(name='First person',presentation_mode='owner_first_person',spoken_name='Alex'))
    m.rules.save(RuleSet(revision=0,rules=[rule(then=Actions(personality_id=automatic['personality_id'],greeting='Office sample.'))]))
    result=asyncio.run(m.place_call(CallRequest(phone_number=NUMBER,objective='Fictional request',personality_id=me['personality_id'])))
    cid=result['call_id'];r=m.voice_record(cid)
    assert r['personality']['personality_id']==me['personality_id'] and r['personality']['outbound_greeting']==''
    assert 'SPEAK AS ME' in session_config(m.settings,r)['instructions']
    snapshot=m.rules.for_call(cid)
    m.rules.save(RuleSet(revision=1,rules=[]))
    assert m.rules.for_call(cid)==snapshot and m.voice_record(cid)['personality']==r['personality']
    assert len(g.calls)==1


def test_mobile_caller_id_rule_and_per_call_override(service):
    _,m,g=service;m.settings.owner_transfer_number='+12025550109'
    m.rules.save(RuleSet(revision=0,rules=[rule(then=Actions(caller_id='mobile'))]))
    a=asyncio.run(m.place_call(CallRequest(phone_number=NUMBER,objective='Check')))
    assert a['origination']['caller_id']=='mobile' and g.from_numbers==['+12025550109']
    b=asyncio.run(m.place_call(CallRequest(phone_number=NUMBER,objective='Check',caller_id='twilio')))
    assert b['origination']['caller_id']=='twilio'


def test_test_rules_need_exact_unique_contacts_and_dont_expand_disclosure(service):
    _,m,_=service;person(m)
    with pytest.raises(ValueError,match='exact saved'):m.rules.save(RuleSet(revision=0,rules=[rule(when=Conditions(),then=Actions(test_mode=True))]))
    m.rules.save(RuleSet(revision=0,rules=[rule(then=Actions(test_mode=True,test_instructions='Harmless fictional tests.'))]))
    f=fact(m,'family',text='FAMILY_PRIVATE_SENTINEL')
    m.settings.caller_id_trust_saved_contacts=True
    cid=call(m)
    assert m.voice_record(cid)['test_mode']['enabled']
    assert 'FAMILY_PRIVATE_SENTINEL' not in session_config(m.settings,m.voice_record(cid))['instructions']
    assert not m.knowledge.search('',call_id=cid,caller_number=NUMBER)['facts']


def test_ai_proposal_requires_review_and_stale_or_unresolved_drafts_cannot_apply(service):
    _,m,g=service;before=m.rules.get()
    async def reviewer(prompt,provider):
        assert provider=='codex' and 'No tools' in prompt
        assert 'private fact lookup' not in prompt or 'OWNER' in prompt
        return ReviewResult(rules=[rule()],explanation='First matching rule wins.',unresolved=[])
    m.rules.reviewer=reviewer
    draft=asyncio.run(m.rules.review(ReviewRequest(instructions='Use the default OpenAI voice for this fictional number.')))
    assert not draft['applied'] and m.rules.get()['revision']==before['revision']
    result=m.rules.apply_review(draft['draft_id']);assert result['revision']==1
    with pytest.raises(ValueError,match='changed elsewhere'):m.rules.apply_review(draft['draft_id'])
    async def unresolved(*args):return ReviewResult(rules=[],explanation='Unsupported request.',unresolved=['Pay money is unsupported'])
    m.rules.reviewer=unresolved
    draft=asyncio.run(m.rules.review(ReviewRequest(instructions='Pay any charges.')))
    with pytest.raises(ValueError,match='warnings'):m.rules.apply_review(draft['draft_id'])
    assert m.rules.get()['revision']==1 and not g.calls
    with pytest.raises(ValidationError):Actions.model_validate({'shell_command':'run something'})


@pytest.mark.parametrize('group,allowed',[('general',{'general'}),('friends',{'general','friends'}),('family',{'general','friends','family'})])
def test_audience_filtering_and_contact_scope(service,group,allowed):
    _,m,_=service;m.settings.caller_id_trust_saved_contacts=True;person(m,group)
    for audience in ('general','friends','family','approval_required'):fact(m,audience,text=audience+'_VALUE')
    cid=call(m)
    values=m.knowledge.search('',caller_number=NUMBER,call_id=cid)['facts']
    assert {f['audience'] for f in values}==allowed
    assert len(m.knowledge.search(owner=True)['facts'])==4
    record=m.voice_record(cid)
    assert 'approval_required_VALUE' not in session_config(m.settings,record)['instructions']
    assert m.disclosures.topics(record)


def test_approval_releases_exact_fact_for_only_one_active_call_and_audits_decision(service):
    _,m,_=service;m.settings.live_tools_enabled=True
    f=fact(m,'approval_required',text='fictional@example.test')
    cid=call(m);other=call(m,'+12025550103')
    request=m.disclosures.request(cid,DisclosureRequest(fact_id=f.fact_id,reason='Caller requests the owner email.'))
    assert request['status']=='pending' and 'fictional@' not in str(request)
    pending=m.disclosures.list(cid)['approvals'][0]
    assert pending['text']=='fictional@example.test' and pending['actionable']
    result=asyncio.run(m.disclosures.resolve(request['approval_id'],True))
    assert result['status']=='approved'
    assert m.knowledge.search('',call_id=cid)['facts'][0]['text']=='fictional@example.test'
    assert not m.knowledge.search('',call_id=other)['facts']
    assert 'fictional@example.test' in m.db.get(cid)['call_updates'][0]['content']
    assert len(m.db.get(cid)['call_updates'])==1
    asyncio.run(m.disclosures.resolve(request['approval_id'],True))
    assert len(m.db.get(cid)['call_updates'])==1
    m.db.transition(cid,'completed')
    assert not m.knowledge.search('',call_id=cid)['facts']


@pytest.mark.parametrize('change',['fact','retire','ended'])
def test_stale_fact_or_ended_call_cannot_be_approved(service,change):
    _,m,_=service;f=fact(m,'approval_required');cid=call(m)
    request=m.disclosures.request(cid,DisclosureRequest(fact_id=f.fact_id,reason='A fictional request.'))
    if change=='fact':m.knowledge.fact(f.model_copy(update={'text':'Changed value'}))
    elif change=='retire':m.knowledge.retire(str(f.fact_id))
    else:m.db.transition(cid,'completed')
    with pytest.raises(ValueError,match='no longer valid'):asyncio.run(m.disclosures.resolve(request['approval_id'],True))
    assert not m.db.get(cid)['call_updates']


def test_group_revocation_and_history_do_not_replay_private_details(service):
    _,m,_=service;m.settings.caller_id_trust_saved_contacts=True;m.settings.call_history_enabled=True
    p=person(m,'family');fact(m,'family',text='PRIVATE_FAMILY_REFERENCE')
    cid=call(m)
    m.db.transcript(cid,{'event_id':'private','speaker':'assistant','start_ms':0,'end_ms':1,'delta':'PRIVATE_FAMILY_REFERENCE'})
    m.knowledge.contact(ContactUpdate(contact_id=p.contact_id,name=p.name,phone_number=NUMBER,information_level='general'))
    assert not m.knowledge.search('',call_id=cid)['facts']
    m.db.transition(cid,'completed');new=call(m)
    assert 'PRIVATE_FAMILY_REFERENCE' not in str(m.memory.candidates(m.db.get(new)))


def test_owner_control_routes_mcp_and_readonly_preview(service):
    c,m,g=service;headers=owner_headers(c)
    assert c.get('/live/rules').status_code==401
    assert c.put('/live/rules',json={'revision':0,'rules':[]}).status_code==401
    assert c.put('/live/rules',headers=headers,json={'revision':0,'rules':[]}).status_code==200
    assert c.post('/live/rules/preview',headers=headers,json={'phone_number':NUMBER}).status_code==200
    cid=call(m)
    assert c.post('/live/calls/'+cid+'/updates',headers=headers,json={'content':'A fictional update.'}).status_code==200
    server,_=create_mcp(m)
    async def run():
        tools={t.name for t in await server.list_tools()}
        assert {'get_call_rules','set_call_rules','review_call_rules','apply_reviewed_call_rules','preview_call_rule',
                'get_outbound_settings','set_outbound_defaults','get_call_approvals','request_call_disclosure','resolve_call_approval'}<=tools
        for name,args in [('get_call_rules',{}),('preview_call_rule',{}),('get_outbound_settings',{})]:
            assert not (await server.call_tool(name,args)).is_error
    asyncio.run(run())
    preview=prompt_preview(m,Preview())
    assert preview['presentation']['mode']=='assistant' and not g.calls


@pytest.mark.parametrize('provider',['openai','elevenlabs','gemini'])
def test_provider_independent_first_person_identity_and_private_fact_filter(service,provider):
    _,m,g=service;configure(m)
    from pydantic import SecretStr
    m.settings.gemini_api_key=SecretStr('fake');m.settings.gemini_live_verified=True;m.settings.live_tools_enabled=True
    p=m.personalities.save(PersonalityUpdate(name='Fictional me',presentation_mode='owner_first_person',spoken_name='Alex'))
    fact(m,'approval_required',text='PRIVATE_EMAIL_SENTINEL')
    m.rules.save(RuleSet(revision=0,rules=[rule(then=Actions(provider=provider,personality_id=p['personality_id']))]))
    cid=call(m);m.voice_backends.pin(m.db.get(cid));record=m.voice_record(cid);config=session_config(m.settings,record)
    assert record['voice_backend']['provider']==provider
    assert 'SPEAK AS ME' in config['instructions'] and 'PRIVATE_EMAIL_SENTINEL' not in config['instructions']
    assert any(t['name']=='request_disclosure_approval' for t in config['delegation']['responses']['tools'])
    assert not g.calls


def test_owner_approval_snapshot_and_no_reuse_in_history(service):
    _,m,_=service;m.settings.call_history_enabled=True
    f=fact(m,'approval_required',text='original@example.test');cid=call(m)
    request=m.disclosures.request(cid,DisclosureRequest(fact_id=f.fact_id,reason='Exact email requested.'))
    m.knowledge.fact(f.model_copy(update={'text':'replacement@example.test'}))
    pending=m.disclosures.list(cid)['approvals'][0]
    assert pending['text']=='original@example.test' and not pending['actionable']
    fresh=m.disclosures.request(cid,DisclosureRequest(fact_id=f.fact_id,reason='Updated email requested.'))
    asyncio.run(m.disclosures.resolve(fresh['approval_id'],True))
    m.db.transcript(cid,{'event_id':'private','speaker':'assistant','start_ms':0,'end_ms':1,'delta':'replacement@example.test'})
    m.db.transition(cid,'completed');new=call(m)
    assert 'replacement@example.test' not in str(m.memory.candidates(m.db.get(new)))


def test_same_family_caller_retains_ordinary_history(service):
    _,m,_=service;m.settings.caller_id_trust_saved_contacts=True;person(m,'family')
    a=call(m);m.db.transition(a,'completed');b=call(m)
    assert not m.disclosures.history_private(a,b)
    c=call(m,'+12025550103');assert m.disclosures.history_private(a,c)


def test_failed_reviewer_does_not_apply_rules_or_expose_private_facts(service):
    _,m,g=service;fact(m,'approval_required',text='PRIVATE_FACT_SENTINEL')
    async def broken(prompt,provider):
        assert provider=='openai' and 'PRIVATE_FACT_SENTINEL' not in prompt
        raise RuntimeError('private-provider-error')
    m.rules.reviewer=broken
    with pytest.raises(ValueError,match='no rules changed') as error:
        asyncio.run(m.rules.review(ReviewRequest(instructions='Use a fictional rule.',provider='openai')))
    assert 'private-provider-error' not in str(error.value) and m.rules.get()['revision']==0 and not g.calls


def test_live_controls_require_exact_origin_and_keep_terminal_calls_closed(service):
    c,m,g=service;c.headers.pop('Authorization');headers=owner_headers(c)
    f=fact(m,'approval_required');cid=call(m)
    body={'fact_id':str(f.fact_id),'reason':'Email requested.'}
    assert c.post('/live/calls/'+cid+'/approvals',headers={**headers,'Origin':'https://other.test'},json=body).status_code==403
    request=c.post('/live/calls/'+cid+'/approvals',headers=headers,json=body).json()
    m.db.transition(cid,'completed')
    assert c.post('/live/approvals/'+request['approval_id'],headers=headers,json={'approve':True}).status_code==409
    assert c.post('/live/calls/'+cid+'/updates',headers=headers,json={'content':'Try an update.'}).status_code==409
    assert not g.calls


def test_api_reviewer_uses_typed_output_without_activation(service,monkeypatch):
    _,m,g=service;seen={}
    class Client:
        def __init__(self,**kw):seen.update(kw);self.responses=self
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def parse(self,**kw):
            seen.update(kw)
            return SimpleNamespace(output_parsed=ReviewResult(rules=[rule()],explanation='Fictional proposal.',unresolved=[]))
    monkeypatch.setattr('openai.AsyncOpenAI',Client)
    result=asyncio.run(m.rules.review(ReviewRequest(instructions='Route the fictional number.',provider='openai')))
    assert not result['applied'] and m.rules.get()['revision']==0
    assert seen['text_format'] is ReviewResult and seen['store'] is False and seen['max_retries']==0
    assert not g.calls


def test_external_agent_can_place_with_explicit_mobile_and_owner_personality(service):
    _,m,g=service;m.settings.owner_transfer_number='+12025550109'
    p=m.personalities.save(PersonalityUpdate(name='Fictional owner',presentation_mode='owner_first_person',spoken_name='Alex'))
    server,_=create_mcp(m)
    result=asyncio.run(server.call_tool('place_call',{'phone_number':NUMBER,'objective':'Fictional check',
        'caller_id':'mobile','personality_id':p['personality_id'],'request_id':str(uuid4())}))
    assert not result.is_error and len(g.calls)==1 and g.from_numbers==['+12025550109']
    profile=m.voice_record(g.calls[0][0])['personality']
    assert profile['presentation_mode']=='owner_first_person' and profile['personality_id']==p['personality_id']
