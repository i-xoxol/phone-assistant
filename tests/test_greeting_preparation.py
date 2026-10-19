import asyncio
import json
import pytest
from types import SimpleNamespace
from datetime import timedelta

from app.calling.context import utcnow
from app.calling.knowledge import ContactUpdate,FactUpdate
from app.calling.lookup import Selection
from app.calling.prompts import session_config
from tests.test_calls import service
from app.calling.live_session import MediaBridge
from tests.test_live import FakeConnection


@pytest.mark.parametrize('variant',['unknown','closed','resume'])
def test_every_incoming_opening_starts_without_caller_speech(service,variant):
    _,m,_=service
    cid=m.db.create_inbound('CA_STARTUP','+12025550102',5,10,'messages')
    record=m.voice_record(cid)
    if variant=='closed': record['closed_announcement']='Office is closed.'
    if variant=='resume': record['transcript']=[{'speaker':'callee','text':'Hello'}]
    config=session_config(m.settings,record)
    opening=config['input'][0]['content'][0]['text']
    assert opening.startswith('Begin speaking immediately when this session starts')
    assert 'Do not wait for caller speech' in opening
    assert 'before waiting for the caller' in config['instructions']
    if variant=='closed': assert 'Do not repeat it' in opening
    if variant=='resume': assert 'without a new introduction' in opening


def setup(m):
    m.settings.caller_id_trust_saved_contacts=True
    m.settings.subscription_lookup_enabled=True
    m.settings.inbound_answer_delay_seconds=8
    contact=ContactUpdate(name='Mama',phone_number='+12025550101',preferred_language='uk',conversation_style='familiar')
    m.knowledge.contact(contact)
    cid=m.db.create_inbound('CA_PREFLIGHT','+12025550101',5,10,'concierge')
    return cid,contact


@pytest.mark.parametrize('already_speaking',['neither','caller','assistant'])
def test_silent_start_gets_only_one_phone_safe_greeting_nudge(service,monkeypatch,already_speaking):
    _,m,_=service;cid,_=setup(m);m.settings.assistant_name='Rocky'
    connection=FakeConnection();sent=[]
    async def append(**kwargs): sent.append(kwargs)
    connection.session.commentary=SimpleNamespace(append=append)
    bridge=MediaBridge(None,connection,m,cid,'MZ_TEST')
    bridge.caller_spoke=already_speaking=='caller';bridge.assistant_spoke=already_speaking=='assistant'
    monkeypatch.setattr('app.calling.live_session.GREETING_NUDGE_SECONDS',0)
    async def exercise():
        await bridge.nudge_greeting();await bridge.nudge_greeting()
    asyncio.run(exercise())
    assert len(sent)==(1 if already_speaking=='neither' else 0)
    if sent:
        assert 'Привіт' in sent[0]['content'] and 'Rocky' in sent[0]['content']
        assert 'мама' not in sent[0]['content'].lower() and 'Mama' not in sent[0]['content']
        assert sent[0]['delegation_id'] is None


def test_luna_prepares_context_before_media_and_never_changes_pickup_delay(service):
    _,m,g=service;cid,_=setup(m)
    async def choose(question,facts,timeout):
        assert 0<timeout<8 and 'personalized' in question
        assert not m.bridges and m.db.get(cid)['status']=='ringing'
        identity=next(f for f in facts if f.get('kind')=='caller_identity')
        return Selection(selected_ids=[identity['fact_id']],needs_supervisor=False)
    m.lookup.runner=choose
    async def run():
        m.greetings.start(cid);m.greetings.start(cid)
        assert len(m.lookup.tasks)==1
        await asyncio.gather(*list(m.lookup.tasks.values()))
    asyncio.run(run())
    config=session_config(m.settings,m.voice_record(cid))
    opening=config['input'][0]['content'][0]['text']
    assert 'Speak Ukrainian from the very first word' in opening
    assert "owner's family, not yours" in opening
    assert 'Mama' in opening and m.settings.caller_name in opening
    assert opening.startswith('Begin speaking immediately when this session starts')
    assert 'Do not wait for caller speech' in opening
    assert 'Prepared background for this caller' in config['instructions']
    assert len(m.lookup.list_for_call(cid))==1
    assert m.settings.inbound_answer_delay_seconds==8 and not g.calls


def test_preparation_timeout_still_greets_known_caller_in_preferred_language(service):
    _,m,_=service;cid,_=setup(m)
    async def timeout(*_): raise TimeoutError()
    m.lookup.runner=timeout
    async def run():
        m.greetings.start(cid)
        await asyncio.gather(*list(m.lookup.tasks.values()))
    asyncio.run(run())
    assert not m.greetings.snapshot(m.db.get(cid))
    assert m.lookup.list_for_call(cid)[0]['error_code']=='timeout'
    opening=session_config(m.settings,m.voice_record(cid))['input'][0]['content'][0]['text']
    assert 'Speak Ukrainian from the very first word' in opening
    assert 'even if the caller is silent' in opening


def test_contact_language_updates_preserve_unmentioned_preference_and_other_callers(service):
    _,m,_=service;cid,contact=setup(m)
    m.knowledge.contact(ContactUpdate(contact_id=contact.contact_id,name='Mama',phone_number=contact.phone_number))
    assert m.knowledge.matching_contacts(contact.phone_number)[0]['preferred_language']=='uk'
    m.transition(cid,'completed')
    other=m.db.create_inbound('CA_OTHER','+12025550102',5,10,'messages')
    opening=session_config(m.settings,m.voice_record(other))['input'][0]['content'][0]['text']
    assert 'Ukrainian' not in opening and 'English' in opening
    m.knowledge.contact(ContactUpdate(contact_id=contact.contact_id,name='Mama',phone_number=contact.phone_number,preferred_language=''))
    assert m.knowledge.matching_contacts(contact.phone_number)[0]['preferred_language']==''


def test_retired_prepared_information_is_withheld(service):
    _,m,_=service;cid,_=setup(m)
    fact=FactUpdate(topic='Anticipated topic',text='The demonstration is on Monday.',source='Owner',
        approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1))
    m.knowledge.fact(fact)
    async def choose(*_): return Selection(selected_ids=[str(fact.fact_id)],needs_supervisor=False)
    m.lookup.runner=choose
    async def run():
        m.greetings.start(cid);await asyncio.gather(*list(m.lookup.tasks.values()))
    asyncio.run(run())
    assert m.greetings.snapshot(m.db.get(cid))
    m.knowledge.retire(str(fact.fact_id))
    assert not m.greetings.snapshot(m.db.get(cid))


def test_mcp_can_set_and_retrieve_contact_language(service):
    from app.mcp.server import create_mcp
    _,m,_=service
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    server,_=create_mcp(m)
    async def run():
        result=await server.call_tool('set_call_contact',{'name':'Mama','phone_number':'+12025550101','preferred_language':'uk'})
        assert not result.is_error
        result=await server.call_tool('search_call_knowledge',{})
        contacts=json.loads(result.content[0].text)['contacts']
        assert contacts[0]['preferred_language']=='uk'
    asyncio.run(run())


def test_spoken_name_and_style_persist_through_mcp_and_language_updates(service):
    from app.mcp.server import create_mcp
    _,m,_=service
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    m.settings.caller_id_trust_saved_contacts=True
    server,_=create_mcp(m)
    async def run():
        result=await server.call_tool('set_call_contact',{'name':'Anne Example','phone_number':'+12025550101',
            'preferred_name':'Annie','conversation_style':'familiar'})
        assert not result.is_error
        cid=json.loads(result.content[0].text)['contact_id']
        # An ordinary language update must preserve the nickname and style.
        result=await server.call_tool('set_call_contact',{'contact_id':cid,'name':'Anne Example',
            'phone_number':'+12025550101','preferred_language':'en'})
        assert not result.is_error
        contact=m.knowledge.search(owner=True)['contacts'][0]
        assert contact['preferred_name']=='Annie' and contact['conversation_style']=='familiar'
        call=m.db.create_inbound('CA_NICKNAME','+12025550101',5,10,'concierge')
        config=session_config(m.settings,m.voice_record(call))
        opening=config['input'][0]['content'][0]['text']
        assert '"Annie"' in opening and 'warmly and informally' in opening
        assert 'Begin speaking immediately' in opening
        assert 'office of' not in opening
        for prompt in (config['instructions'],config['delegation']['responses']['instructions']):
            assert 'CONVERSATION STYLE: Familiar' in prompt
            assert 'whole conversation warm, relaxed and informal' in prompt
    asyncio.run(run())


@pytest.mark.parametrize('saved,trust',[(False,True),(True,True),(True,False)])
def test_unknown_and_business_callers_use_configured_formal_office(service,saved,trust):
    from app.calling.hours import HoursSettings
    _,m,_=service
    m.settings.caller_id_trust_saved_contacts=trust
    m.hours.set(HoursSettings(office_name='the office of Dr. Alex'))
    if saved:
        m.knowledge.contact(ContactUpdate(name='Example Clinic',phone_number='+12025550101',
            conversation_style='formal' if trust else 'familiar',preferred_name='Clinic'))
    cid=m.db.create_inbound('CA_FORMAL','+12025550101',5,10,'messages')
    config=session_config(m.settings,m.voice_record(cid))
    assert 'CONVERSATION STYLE: Formal' in config['instructions']
    assert "Hello, you've reached the office of Dr. Alex" in config['input'][0]['content'][0]['text']


def test_changing_style_and_clearing_nickname_survives_reopen(service):
    from app.calling.knowledge import CallKnowledge
    _,m,_=service
    contact=ContactUpdate(name='Example Friend',phone_number='+12025550101',preferred_name='Sam',conversation_style='familiar')
    m.knowledge.contact(contact)
    m.knowledge.contact(ContactUpdate(contact_id=contact.contact_id,name=contact.name,
        phone_number=contact.phone_number,preferred_name='',conversation_style='formal'))
    found=CallKnowledge(m.db).matching_contacts(contact.phone_number)[0]
    assert found['preferred_name']=='' and found['conversation_style']=='formal'
