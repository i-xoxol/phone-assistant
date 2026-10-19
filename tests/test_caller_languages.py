import asyncio
import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.live import COOKIE
from app.calling.inbound import startup_greeting_content
from app.calling.knowledge import CallKnowledge,ContactUpdate
from app.calling.languages import CallerLanguage,remember_language
from app.calling.prompts import session_config
from app.calling.context import utcnow
from app.mcp.server import create_mcp
from app.storage.database import Database
from tests.test_calls import service
from tests.test_lookup import setup_call


def request(m,cid,text='Can we speak Ukrainian?'):
    m.db.transcript(cid,{'event_id':str(uuid4()),'type':'session.input_transcript.delta',
        'start_ms':0,'end_ms':100,'delta':text})


def test_explicit_unknown_caller_choice_persists_and_changes_next_greeting(service):
    client,m,_=service
    cid,bridge=setup_call(client,m)
    assert m.knowledge.language_for('+12025550101')['language']=='en'
    request(m,cid)
    async def run():
        item={'name':'remember_caller_language','call_id':'language-one',
              'arguments':json.dumps({'language':'uk','request_quote':'Can we speak Ukrainian?'})}
        result=await bridge.tools.operation(item)
        assert result['remembered'] and result['language']=='uk'
        assert (await bridge.tools.operation(item))['status']=='already_requested'
    asyncio.run(run())
    restarted=CallKnowledge(Database(m.db.path))
    assert restarted.language_for('+12025550101')['language']=='uk'
    m.transition(cid,'completed')
    next_id=m.db.create_inbound('CA_NEXT_LANGUAGE','+12025550101',5,10,'concierge')
    record=m.voice_record(next_id)
    assert not record['incoming_context']['caller']['identity_verified']
    assert record['incoming_context']['caller']['saved_contacts']==[]
    assert 'Вітаю' in startup_greeting_content(m.settings,record)
    opening=session_config(m.settings,record)['input'][0]['content'][0]['text']
    assert 'Speak Ukrainian from the very first word' in opening and 'Speak English from' not in opening
    assert restarted.caller_book()[0]['name']==''  # no invented contact identity


def test_assistant_or_missing_quote_cannot_create_a_preference(service):
    client,m,_=service;cid,_=setup_call(client,m)
    m.db.transcript(cid,{'event_id':'assistant-only','type':'session.output_transcript.delta',
        'start_ms':0,'end_ms':100,'delta':'Can we speak Ukrainian?'})
    result=remember_language(m,cid,CallerLanguage(language='uk',request_quote='Can we speak Ukrainian?'))
    assert not result['remembered'] and result['status']=='unconfirmed'
    assert m.knowledge.language_for('+12025550101')['language']=='en'
    with pytest.raises(ValidationError):
        CallerLanguage(language='uk',request_quote='A real request',phone_number='+12025550109')


@pytest.mark.parametrize('ambiguous',[False,True])
def test_missing_or_ambiguous_number_is_this_call_only(service,ambiguous):
    client,m,_=service;cid,_=setup_call(client,m)
    if ambiguous:
        # Presented number equals the forwarding leg, so it cannot identify the original caller.
        m.settings.owner_transfer_number='+12025550101'
        with m.db.connect() as db:
            db.execute('UPDATE incoming_routing SET forwarded_from=?,forwarding_reported=1 WHERE call_id=?',('+12025550101',cid))
    else:
        with m.db.connect() as db: db.execute("UPDATE calls SET phone_number='' WHERE call_id=?",(cid,))
    request(m,cid)
    result=remember_language(m,cid,CallerLanguage(language='uk',request_quote='Can we speak Ukrainian?'))
    assert not result['remembered'] and result['status']=='this_call_only'
    record=m.voice_record(cid)
    assert record['incoming_context']['caller']['language_preference']['language']=='uk'
    assert m.knowledge.caller_book()==[]


def test_owner_can_override_without_changing_familiar_identity(service):
    client,m,_=service;cid,_=setup_call(client,m)
    m.knowledge.contact(ContactUpdate(name='Anne Example',phone_number='+12025550101',
        preferred_name='Annie',conversation_style='familiar',preferred_language='uk'))
    request(m,cid)
    remember_language(m,cid,CallerLanguage(language='uk',request_quote='Can we speak Ukrainian?'))
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    server,_=create_mcp(m)
    async def run():
        result=await server.call_tool('set_caller_language',{'phone_number':'+12025550101','language':'en'})
        assert not result.is_error
        data=json.loads((await server.call_tool('search_call_knowledge',{})).content[0].text)
        assert data['caller_preferences'][0]['language']=='en'
    asyncio.run(run())
    contact=m.knowledge.matching_contacts('+12025550101')[0]
    assert contact['preferred_language']=='en' and contact['preferred_name']=='Annie'
    assert contact['conversation_style']=='familiar'
    assert m.incoming_context(m.db.get(cid))['caller']['language_preference']['language']=='en'


def test_private_portal_book_and_language_change_require_auth_and_csrf(service):
    client,m,_=service;client.base_url='https://example.test'
    path='/live/caller-language';body={'phone_number':'+12025550101','language':'es'}
    assert client.put(path,json=body).status_code==401
    token=client.app.state.web_auth.login(client.app.state.web_auth.create_code())
    client.cookies.set(COOKIE,token)
    assert client.put(path,json=body).status_code==403
    csrf=client.app.state.web_auth.session(token)['csrf']
    assert client.put(path,json=body,headers={'Origin':'https://example.test','X-CSRF-Token':csrf}).status_code==200
    assert client.get('/live/bootstrap').json()['caller_book'][0]['language']=='es'
    assert 'Caller book' in client.get('/live').text
    assert client.put(path,json={**body,'language':'not a language'},
        headers={'Origin':'https://example.test','X-CSRF-Token':csrf}).status_code==422


def test_full_directory_keeps_language_for_this_call_without_failing_it(service):
    client,m,_=service;cid,_=setup_call(client,m)
    with m.db.connect() as db:
        db.executemany('INSERT INTO caller_languages VALUES (?,?,?,?)',
            [('+1'+str(5550000000+i),'en','caller',utcnow().isoformat()) for i in range(1000)])
    request(m,cid)
    result=remember_language(m,cid,CallerLanguage(language='uk',request_quote='Can we speak Ukrainian?'))
    assert result['status']=='this_call_only' and not result['remembered']
    assert m.knowledge.language_for('+12025550101',cid)['language']=='uk'
    assert m.db.get(cid)['status']=='in_progress'


@pytest.mark.parametrize('language,prefix',[('es','Hola'),('fr','Bonjour'),('de','Guten Tag'),
    ('pl','Dzień dobry'),('ru','Здравствуйте'),('zh','您好'),('es-MX','Hola'),('sv','Hello')])
def test_remembered_languages_always_have_a_silent_start_greeting(language,prefix):
    from app.config import Settings
    record={'phone_number':'+12025550101','incoming_context':{'caller':{
        'number':'+12025550101','language_preference':{'language':language}}}}
    assert startup_greeting_content(Settings(_env_file=None),record).startswith(prefix)
