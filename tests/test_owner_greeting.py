"""Regression for saved owner preferences losing to startup introductions."""
import asyncio
from types import SimpleNamespace

import pytest

from app.calling.context import ProfileUpdate
from app.calling.inbound import startup_greeting_content
from app.calling.knowledge import ContactUpdate
from app.calling.live_session import MediaBridge
from app.calling.prompts import session_config
from tests.test_calls import service
from tests.test_live import FakeConnection


def owner_call(manager, *, mode='concierge', language='en'):
    manager.settings.caller_id_trust_saved_contacts=True
    manager.settings.owner_transfer_number='+12025550101'
    manager.settings.assistant_name='Rocky'
    manager.settings.caller_name='Alex'
    manager.knowledge.contact(ContactUpdate(name='Alex Example',phone_number='+12025550101',
        preferred_name='Alex',conversation_style='familiar',preferred_language=language))
    return manager.db.create_inbound('CA_OWNER_GREETING','+12025550101',5,10,mode)


@pytest.mark.parametrize('mode',['concierge','messages'])
@pytest.mark.parametrize('language',['en','uk','es','es-MX','fr','ja','zz'])
def test_owner_native_and_fallback_greetings_skip_self_introduction(service,mode,language):
    _,m,_=service;cid=owner_call(m,mode=mode,language=language)
    record=m.voice_record(cid)
    fallback=startup_greeting_content(m.settings,record)
    assert 'Alex' in fallback and 'Rocky' not in fallback and 'office' not in fallback
    if language=='en': assert fallback=="Hi, Alex! What's up?"
    config=session_config(m.settings,record)
    opening=config['input'][0]['content'][0]['text']
    assert 'Greet the recognized owner casually' in opening
    assert 'Skip all self-introduction' in opening
    assert 'your assistant' not in opening and "I'm Rocky" not in opening
    for prompt in (config['instructions'],config['delegation']['responses']['instructions']):
        assert 'OWNER GREETING' in prompt
        assert 'Do not add a second introduction later' in prompt
        assert 'not permissions or disclosure rules' in prompt
    assert "Introduce yourself as Alex's assistant" not in config['instructions']


def test_mcp_saved_greeting_is_in_next_voice_session_without_restarting(service):
    from app.mcp.server import create_mcp
    _,m,_=service;cid=owner_call(m)
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    server,_=create_mcp(m)
    async def save():
        result=await server.call_tool('set_inbound_context',{'role':'Personal concierge',
            'context':'For Alex use "Hi, I am here" without self-introduction. Other callers use the office greeting.'})
        assert not result.is_error
    asyncio.run(save())
    config=session_config(m.settings,m.voice_record(cid))
    assert 'Hi, I am here' in config['instructions']
    opening=config['input'][0]['content'][0]['text']
    assert 'saved owner-approved greeting preference' in opening
    assert 'take precedence over these sample phrases' in opening
    assert 'your assistant' not in startup_greeting_content(m.settings,m.voice_record(cid))


def test_actual_silent_start_commentary_for_owner_has_no_old_introduction(service,monkeypatch):
    _,m,_=service;cid=owner_call(m)
    sent=[];connection=FakeConnection()
    async def append(**kwargs): sent.append(kwargs)
    connection.session.commentary=SimpleNamespace(append=append)
    bridge=MediaBridge(None,connection,m,cid,'MZ_OWNER_GREETING')
    async def run():
        await bridge.nudge_greeting();await bridge.nudge_greeting()
    asyncio.run(run())
    assert len(sent)==1 and sent[0]['content'].endswith("Hi, Alex! What's up?")
    assert sent[0]['content'].startswith('One-time startup opening only.')


@pytest.mark.parametrize('variant',['untrusted','duplicate','mismatched_metadata','forwarding_ambiguous',
                                   'different_presented_number','owner_name_only','blocked_number'])
def test_owner_greeting_requires_exact_recognized_presented_number(service,variant):
    _,m,_=service;cid=owner_call(m)
    record=m.voice_record(cid)
    if variant=='untrusted': m.settings.caller_id_trust_saved_contacts=False
    elif variant=='duplicate':
        record['incoming_context']['caller']['saved_contacts']*=2
    elif variant=='mismatched_metadata':
        record['incoming_context']['caller']['number']='+12025550103'
    elif variant=='forwarding_ambiguous':
        record['incoming_context']['forwarding']['caller_identity_ambiguous']=True
    elif variant=='different_presented_number':
        record['phone_number']='+12025550103'
    elif variant=='owner_name_only':
        record['phone_number']='+12025550103'
        record['incoming_context']['caller']['number']=record['phone_number']
    else:
        record['phone_number']='anonymous'
        record['incoming_context']['caller']['number']=''
    config=session_config(m.settings,record)
    assert 'OWNER GREETING' not in config['instructions']
    assert 'Greet the recognized owner casually' not in config['input'][0]['content'][0]['text']


def test_forwarded_owner_and_third_party_keep_distinct_greetings(service):
    _,m,_=service;cid=owner_call(m)
    record=m.voice_record(cid)
    record['incoming_context']['forwarding']['missed_call_context']=True
    assert "couldn't reach" not in startup_greeting_content(m.settings,record)
    config=session_config(m.settings,record)
    assert 'MISSED-CALL ASSISTANT:' not in config['instructions']
    record['phone_number']='+12025550102'
    record['incoming_context']['caller']={'number':record['phone_number'],'saved_contacts':[]}
    assert "Sorry you couldn't reach" in startup_greeting_content(m.settings,record)
    assert 'MISSED-CALL ASSISTANT:' in session_config(m.settings,record)['instructions']


@pytest.mark.parametrize('variant',['resume','closed'])
def test_owner_greeting_does_not_restart_transferred_call_or_repeat_closed_notice(service,variant):
    _,m,_=service;cid=owner_call(m)
    record=m.voice_record(cid)
    if variant=='resume': record['transcript']=[{'speaker':'callee','text':'Hello'}]
    else: record['closed_announcement']='The office is closed.'
    assert startup_greeting_content(m.settings,record) is None
    opening=session_config(m.settings,record)['input'][0]['content'][0]['text']
    assert ('without a new introduction' if variant=='resume' else 'Do not repeat it') in opening
