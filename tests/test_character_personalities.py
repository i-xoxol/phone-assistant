"""Independent fictional identity is separate from owner representation and permissions."""
import sqlite3
import json

import pytest
from app.api.personalities import Preview,prompt_preview
from app.calling.personalities import PersonalityUpdate
from app.calling.knowledge import ContactUpdate
from app.calling.presentation import resolve_presentation,voice_identity,outbound_opening
from app.calling.inbound import startup_greeting_content
from app.calling.prompts import session_config
from app.calling.elevenlabs_session import realtime_config
from app.calling.gemini_session import gemini_config
from app.storage.character_profiles import allow_character_profiles
from tests.test_calls import service
from tests.test_presentation import record_for
from tests.test_phone_controls import owner_headers


def character(m):
    return m.personalities.save(PersonalityUpdate(name='Jordan Example',presentation_mode='character',
        role='Fictional bicycle mechanic',instructions='Be warm, respectful and plain-spoken.',
        context='Jordan is a fictional bicycle mechanic. Typical evenings are free, not verified availability.'))


@pytest.mark.parametrize('direction',['inbound','outbound'])
@pytest.mark.parametrize('provider',['openai','elevenlabs','gemini'])
def test_character_identity_and_biography_are_consistent_across_providers(service,direction,provider):
    _,m,gateway=service;profile=character(m);record=record_for(m,direction,profile)
    presentation=resolve_presentation(m.settings,record)
    assert presentation['mode']=='character' and presentation['spoken_name']=='Jordan Example'
    config=session_config(m.settings,record)
    if provider=='elevenlabs':text=realtime_config(m.settings,record,{'model':'eleven_realtime_v1_mini','voice':'fixture'})['instructions']
    elif provider=='gemini':text=gemini_config(m.settings,record,{'model':'gemini-3.8-live','voice':'Kore'})['system_instruction']
    else:text=config['instructions']
    assert 'fictional character' in text and 'Jordan Example' in text
    assert 'CHARACTER PERSPECTIVE' in text
    assert 'AI portraying this character' in text
    assert 'Character habits and working hours are background, not verified calendar availability' in text
    assert 'SPEAK AS ME' not in text
    assert "You are Jordan Example, Alex's personal assistant" not in text
    assert 'fictional bicycle mechanic' in text
    identity=voice_identity(m.settings,record,inbound=direction=='inbound')
    assert 'first person' in identity and 'grants no new' in identity
    if direction=='inbound':
        assert startup_greeting_content(m.settings,record)=='Hi, Jordan Example here. How can I help?'
    else:
        opening=outbound_opening(m.settings,record)
        assert 'Jordan Example' in opening and 'Introduce yourself simply as' not in opening
    assert not gateway.calls


def test_api_edit_duplicate_and_snapshot_keep_the_character_identity(service):
    client,m,_=service;headers=owner_headers(client);before=m.personalities.catalog()
    response=client.post('/live/personalities',headers=headers,json={
        'name':'Independent example','presentation_mode':'character','spoken_name':'Jordan',
        'role':'Fictional mechanic','context':'Only the supplied fictional biography.'})
    assert response.status_code==200,response.text
    profile=response.json();pid=profile['personality_id']
    catalog=m.personalities.catalog();assert catalog['default_personality_id']==before['default_personality_id']
    assert catalog['contact_assignments']==before['contact_assignments']
    assert next(p for p in catalog['personalities'] if p['personality_id']==pid)['presentation_mode']=='character'
    preview=client.post('/voice/instructions-preview',json={'personality_id':pid,'direction':'outbound'})
    assert preview.status_code==200 and preview.json()['presentation']['spoken_name']=='Jordan'
    clone=m.personalities.duplicate(pid,'Copied example');assert clone['presentation_mode']=='character'
    record=record_for(m,'outbound',profile)
    changed=m.personalities.save(PersonalityUpdate(name='Renamed',revision=1,context='Revised biography.'),pid)
    assert changed['presentation_mode']=='character' and changed['spoken_name']=='Jordan'
    assert m.personalities.for_call(record['call_id'])['name']=='Independent example'
    assert m.result(record['call_id'])['presentation']['mode']=='character'
    denied=client.put('/live/personalities/'+pid,headers=headers,json={'name':'Lost update','revision':1,'presentation_mode':'assistant'})
    assert denied.status_code==409


def test_character_does_not_widen_tool_permissions(service):
    _,m,_=service;record=record_for(m,'inbound',character(m));config=session_config(m.settings,record)
    other={**record,'personality':{**record['personality'],'presentation_mode':'assistant'}}
    assert config['delegation']['responses']['tools']==session_config(m.settings,other)['delegation']['responses']['tools']
    text=config['instructions']
    assert 'fiction grants no authority' in text and 'owner availability or permission to book' in text


def test_legacy_parent_constraint_migration_preserves_references_indexes_and_rows(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as c:
        c.execute('PRAGMA foreign_keys=ON')
        c.executescript("""
        CREATE TABLE telephone_personalities (id TEXT PRIMARY KEY, name TEXT NOT NULL,
          presentation_mode TEXT NOT NULL DEFAULT 'assistant' CHECK(presentation_mode IN ('assistant','owner_first_person')));
        CREATE INDEX personality_names ON telephone_personalities(name);
        CREATE TABLE assignment(id TEXT PRIMARY KEY, profile TEXT REFERENCES telephone_personalities(id));
        INSERT INTO telephone_personalities VALUES ('one','Saved assistant','assistant'),('two','Saved owner','owner_first_person');
        INSERT INTO assignment VALUES ('contact','one');
        """)
        allow_character_profiles(c)
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert not c.execute('PRAGMA foreign_key_check').fetchall()
        assert c.execute('SELECT * FROM assignment').fetchall()==[('contact','one')]
        assert len(c.execute('SELECT * FROM telephone_personalities').fetchall())==2
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='personality_names'").fetchone()[0]==1
        c.execute("INSERT INTO telephone_personalities VALUES ('three','Character','character')");c.commit()
        allow_character_profiles(c)  # Idempotent, including an existing character.
        assert len(c.execute('SELECT * FROM telephone_personalities').fetchall())==3
        with pytest.raises(sqlite3.IntegrityError):c.execute("INSERT INTO telephone_personalities VALUES ('four','Bad','unknown')")
        c.rollback()
        with pytest.raises(sqlite3.IntegrityError):c.execute("INSERT INTO assignment VALUES ('invalid','missing')")


def test_legacy_migration_rolls_back_if_references_are_already_invalid(tmp_path):
    with sqlite3.connect(tmp_path/'broken.sqlite3') as c:
        c.executescript("""CREATE TABLE telephone_personalities(id TEXT PRIMARY KEY,
          presentation_mode TEXT CHECK(presentation_mode IN ('assistant','owner_first_person')));
          CREATE TABLE assignment(profile TEXT REFERENCES telephone_personalities(id));
          INSERT INTO assignment VALUES ('missing');""")
        with pytest.raises(sqlite3.IntegrityError):allow_character_profiles(c)
        assert 'character' not in c.execute("SELECT sql FROM sqlite_master WHERE name='telephone_personalities'").fetchone()[0]
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1


def test_character_owner_call_does_not_assume_a_personal_relationship(service):
    from tests.test_owner_greeting import owner_call
    _,m,_=service;profile=character(m);m.personalities.set_default(profile['personality_id'])
    cid=owner_call(m);record=m.voice_record(cid)
    opening=startup_greeting_content(m.settings,record)
    assert 'Jordan Example' in opening and m.settings.caller_name not in opening
    assert "owner's assistant" not in opening


@pytest.mark.parametrize('language',['uk','es','ja'])
def test_character_localized_fallback_uses_its_own_name(service,language):
    from tests.test_phone_controls import contact
    _,m,_=service;profile=character(m)
    contact(m,number='+12025550102',name='Fictional caller')
    m.knowledge.set_language('+12025550102',language)
    record=record_for(m,'inbound',profile)
    opening=startup_greeting_content(m.settings,record)
    assert 'Jordan Example' in opening and m.settings.assistant_name not in opening


def test_character_forwarding_does_not_insert_an_owner_missed_call_apology(service):
    from app.api.personalities import Preview,prompt_preview
    _,m,_=service;m.settings.owner_transfer_number='+12025550109'
    profile=character(m)
    cid=m.knowledge.contact(ContactUpdate(name='Fictional neighbor',phone_number='+12025550101'))['contact_id']
    preview=prompt_preview(m,Preview(contact_id=cid,personality_id=profile['personality_id'],direction='inbound',forwarded_from='+12025550109'))
    assert preview['presentation']['mode']=='character'
    startup=json.dumps(preview['startup_input'])
    assert 'This is a missed mobile call' not in startup
    assert 'Jordan Example' in startup
