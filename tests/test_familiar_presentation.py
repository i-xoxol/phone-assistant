"""Known personal callers keep their language, relationship and selected identity."""
import asyncio
from types import SimpleNamespace

import pytest

from app.calling.elevenlabs_session import realtime_config
from app.calling.gemini_session import gemini_config
from app.calling.inbound import startup_greeting_content
from app.calling.knowledge import ContactUpdate
from app.calling.live_session import MediaBridge
from app.calling.personalities import PersonalityUpdate
from app.calling.prompts import session_config
from tests.test_calls import service
from tests.test_contact_guidance import call_record,entry,save
from tests.test_live import FakeConnection


def known_caller(m,number='+12025550101',name='Оля',language='uk'):
    contact=ContactUpdate(name=name+' Example',phone_number=number,preferred_name=name,
        conversation_style='familiar',preferred_language=language)
    m.knowledge.contact(contact)
    m.settings.caller_id_trust_saved_contacts=True
    m.settings.owner_transfer_number='+12025550109'
    m.settings.inbound_missed_call_context=True
    return contact


def owner_profile(m):
    return m.personalities.save(PersonalityUpdate(name='First-person example',presentation_mode='owner_first_person',spoken_name='Alex'))


@pytest.mark.parametrize('provider',['openai','elevenlabs','gemini'])
@pytest.mark.parametrize('forwarded',['','+12025550109'])
def test_familiar_first_person_native_and_fallback_use_saved_language_without_self_introduction(service,provider,forwarded):
    _,m,gateway=service;contact=known_caller(m);profile=owner_profile(m)
    record=call_record(m,profile,'inbound',contact.phone_number,forwarded)
    assert record['incoming_context']['caller']['language_preference']['language']=='uk'
    assert startup_greeting_content(m.settings,record)=='Привіт, Оля! Як ти?'
    config=session_config(m.settings,record)
    opening=config['input'][0]['content'][0]['text']
    assert 'Speak Ukrainian from the very first word' in opening
    assert 'Skip routine self-introductions' in opening
    assert 'Оля' in opening and 'Hello, Alex' not in opening
    if provider=='openai':wire=config['instructions']
    elif provider=='elevenlabs':wire=realtime_config(m.settings,record,{'model':'eleven_realtime_v1_max','voice':'fixture'})['instructions']
    else:wire=gemini_config(m.settings,record,{'model':'gemini-3.8-live','voice':'Kore'})['system_instruction']
    assert 'CALLER LANGUAGE: Use Ukrainian' in wire
    if forwarded:
        assert record['incoming_context']['forwarding']['forwarded_from_owner_mobile']
        assert 'do not add a third-person missed-call apology' in wire
    assert not gateway.calls


def test_actual_silent_start_uses_the_same_familiar_first_person_greeting(service):
    _,m,_=service;contact=known_caller(m);record=call_record(m,owner_profile(m),'inbound',contact.phone_number,'+12025550109')
    sent=[];connection=FakeConnection()
    async def append(**kwargs):sent.append(kwargs)
    connection.session.commentary=SimpleNamespace(append=append)
    bridge=MediaBridge(None,connection,m,record['call_id'],'MZ_FICTIONAL_FAMILY')
    async def run():
        await bridge.nudge_greeting();await bridge.nudge_greeting()
    asyncio.run(run())
    assert len(sent)==1 and sent[0]['content'].endswith('Привіт, Оля! Як ти?')
    assert 'Alex' not in sent[0]['content']


@pytest.mark.parametrize('direction',['inbound','outbound'])
@pytest.mark.parametrize('mode',['assistant','owner_first_person'])
def test_family_and_friend_context_remain_separate_under_each_selected_profile(service,direction,mode):
    _,m,_=service;wife=known_caller(m);friend=known_caller(m,'+12025550102','Ірина')
    me=owner_profile(m);assistant=m.personalities.save(PersonalityUpdate(name='Rocky example',presentation_mode='assistant'))
    selected=me if mode=='owner_first_person' else assistant
    for caller,relationship,opening in ((wife,'FICTIONAL_SPOUSE_OF_ALEX','Привіт, Олю! Як ти?'),
                                        (friend,'FICTIONAL_FRIEND_OF_ALEX','Ірино, привіт! Як справи?')):
        save(m,caller.contact_id,[entry('fact','Relationship',relationship),
            entry('instruction','First person','Use this approved personal relationship in the first person; speak Ukrainian.',
                profile_scope='selected',profile_ids=[me['personality_id']],greeting=opening),
            entry('instruction','Assistant','Speak Ukrainian as Rocky, the owner assistant. Use the owner relationship without becoming the spouse.',
                profile_scope='selected',profile_ids=[assistant['personality_id']],greeting='Привіт! Це Рокі.')])
    for caller,relationship,other in ((wife,'FICTIONAL_SPOUSE_OF_ALEX','FICTIONAL_FRIEND_OF_ALEX'),
                                      (friend,'FICTIONAL_FRIEND_OF_ALEX','FICTIONAL_SPOUSE_OF_ALEX')):
        record=call_record(m,selected,direction,caller.phone_number,'+12025550109' if direction=='inbound' else '')
        config=session_config(m.settings,record)
        assert record['contact_guidance']['contact_id']==str(caller.contact_id)
        assert relationship in config['instructions'] and other not in config['instructions']
        if direction=='inbound':assert startup_greeting_content(m.settings,record)==record['contact_guidance']['greeting']
        m.transition(record['call_id'],'completed')


@pytest.mark.parametrize('language,prefix',[('en','Hi, Оля!'),('uk','Привіт, Оля!'),('es','Hola, Оля.')])
def test_familiar_representative_fallback_omits_self_introduction_in_each_supported_language(service,language,prefix):
    _,m,_=service;caller=known_caller(m,language=language)
    record=call_record(m,owner_profile(m),'inbound',caller.phone_number)
    greeting=startup_greeting_content(m.settings,record)
    assert greeting.startswith(prefix) and 'Alex' not in greeting and 'Rocky' not in greeting


@pytest.mark.parametrize('variant',['duplicate','forwarding_ambiguous','withheld'])
def test_unresolved_identity_never_receives_a_family_greeting(service,variant):
    _,m,_=service;caller=known_caller(m);profile=owner_profile(m)
    save(m,caller.contact_id,[entry('fact','Family','FICTIONAL_FAMILY_ONLY')])
    number=caller.phone_number;forwarded='+12025550109'
    if variant=='duplicate':m.knowledge.contact(ContactUpdate(name='Different person',phone_number=number))
    elif variant=='forwarding_ambiguous':forwarded=number
    else:number=''
    record=call_record(m,profile,'inbound',number,forwarded)
    assert 'Оля' not in startup_greeting_content(m.settings,record)
    assert record['contact_guidance']['contact_id'] is None
    assert 'FICTIONAL_FAMILY_ONLY' not in session_config(m.settings,record)['instructions']


def test_familiar_contact_does_not_turn_an_independent_character_into_the_owner(service):
    _,m,_=service;caller=known_caller(m)
    character=m.personalities.save(PersonalityUpdate(name='Jordan Example',presentation_mode='character'))
    record=call_record(m,character,'inbound',caller.phone_number,'+12025550109')
    assert startup_greeting_content(m.settings,record)=='Привіт, це Jordan Example. Чим можу допомогти?'
    assert 'Skip routine self-introductions' not in session_config(m.settings,record)['input'][0]['content'][0]['text']


def test_resume_does_not_repeat_the_family_opening(service):
    _,m,_=service;caller=known_caller(m)
    record=call_record(m,owner_profile(m),'inbound',caller.phone_number)
    record['transcript']=[{'speaker':'callee','text':'Я питала про наші плани.','timestamp':0,'end_timestamp':1}]
    assert startup_greeting_content(m.settings,record) is None
    assert 'Continue this existing call' in session_config(m.settings,record)['input'][0]['content'][0]['text']
