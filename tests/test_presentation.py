"""Presentation is a pinned voice preference, never an action/identity grant."""
import asyncio
import json
from uuid import uuid4

import pytest

from app.api.personalities import Preview, prompt_preview
from app.calling.context import ProfileUpdate
from app.calling.elevenlabs_session import realtime_config
from app.calling.gemini_session import gemini_config
from app.calling.inbound import startup_greeting_content
from app.calling.personalities import Personalities, PersonalityUpdate
from app.calling.presentation import outbound_opening
from app.calling.prompts import session_config
from app.calling.style import LEGACY_IDENTITY_STYLE_LINE
from app.mcp.server import create_mcp
from tests.test_calls import service, create
from tests.test_owner_greeting import owner_call
from tests.test_phone_controls import owner_headers, contact


def saved(m, mode='owner_first_person', **changes):
    return m.personalities.save(PersonalityUpdate(**dict(name='Fictional representative',
        presentation_mode=mode, spoken_name='Alex', role='Personal correspondence',
        instructions='Speak concisely and warmly.', **changes)))


def record_for(m, direction, profile):
    m.personalities.set_default(profile['personality_id'])
    if direction == 'inbound':
        cid = m.db.create_inbound('CA'+uuid4().hex, '+12025550102', 5, 10, 'concierge', False)
    else:
        from app.storage.models import CallRequest
        cid = m.db.create(CallRequest(phone_number='+12025550102', objective='Ask about my appointment'))
    return m.voice_record(cid)


@pytest.mark.parametrize('direction', ['inbound', 'outbound'])
@pytest.mark.parametrize('mode', ['assistant', 'owner_first_person'])
@pytest.mark.parametrize('provider', ['openai', 'elevenlabs', 'gemini'])
def test_direction_mode_and_provider_share_identity_and_permissions(service, direction, mode, provider):
    _, m, gateway = service
    m.settings.live_tools_enabled = True
    m.settings.owner_transfer_enabled = True
    m.settings.assistant_name = 'Rocky'
    record = record_for(m, direction, saved(m, mode))
    config = session_config(m.settings, record)
    text = config['instructions']
    if provider == 'elevenlabs':
        text = realtime_config(m.settings, record, {'model': 'eleven_realtime_v1_mini', 'voice': 'test-clone'})['instructions']
    elif provider == 'gemini':
        text = gemini_config(m.settings, record, {'model': 'gemini-3.8-live', 'voice': 'Kore'})['system_instruction']
    if mode == 'owner_first_person':
        assert 'SPEAK AS ME' in text and 'Use I/my' in text
        assert 'explain honestly' in text and 'actual human being present' in text
        assert 'verification requirements' in text and 'PARTICIPANT PERSPECTIVE' in text
        assert "Introduce yourself as Alex's assistant" not in text
        assert 'Never impersonate' not in text
        assert 'caller asking about "my calendar"' in text
        assert 'automated representative and you will try the actual owner' in text
        other = {**record, 'personality': {**record['personality'], 'presentation_mode': 'assistant'}}
        assert config['delegation']['responses']['tools'] == session_config(m.settings, other)['delegation']['responses']['tools']
    else:
        assert "Alex's personal assistant" in text
        assert 'Never impersonate' in text
        assert 'SPEAK AS ME' not in text
    assert 'no new' in text.lower() or 'grant no' in text.lower()
    assert not gateway.calls


def test_update_omissions_duplicate_revision_and_call_snapshot(service):
    c, m, _ = service
    profile = saved(m, outbound_greeting='State the appointment objective immediately.')
    m.personalities.set_default(profile['personality_id'])
    cid = create(c).json()['call_id']
    snapshot = m.personalities.for_call(cid)
    changed = m.personalities.save(PersonalityUpdate(name='Renamed', revision=1), profile['personality_id'])
    for key in ('role', 'presentation_mode', 'spoken_name', 'outbound_greeting'):
        assert changed[key] == profile[key]
    clone = m.personalities.duplicate(profile['personality_id'], 'Independent copy')
    for key in ('role', 'presentation_mode', 'spoken_name', 'outbound_greeting'):
        assert clone[key] == profile[key]
    m.personalities.save(PersonalityUpdate(name='Assistant again', revision=2, presentation_mode='assistant'), profile['personality_id'])
    m.context.set_profile(ProfileUpdate(context='Fresh information, same identity.'))
    assert m.voice_record(cid)['personality'] == snapshot
    assert m.result(cid)['presentation']['mode'] == 'owner_first_person'
    m.db.transcript(cid, {'speaker':'assistant', 'event_id':'fictional-turn', 'start_ms':1000, 'end_ms':2000, 'delta':'Hello.'})
    assert m.result(cid)['transcript'][0]['speaker'] == 'assistant'
    assert m.result(cid)['presentation']['spoken_name'] == 'Alex'


@pytest.mark.parametrize('language', ['en', 'uk', 'es', 'ja'])
def test_real_owner_greeting_remains_familiar_in_first_person_mode(service, language):
    _, m, _ = service
    m.personalities.set_default(saved(m)['personality_id'])
    cid = owner_call(m, language=language)
    record = m.voice_record(cid)
    greeting = startup_greeting_content(m.settings, record)
    assert 'Alex' in greeting and 'Rocky' not in greeting and 'office' not in greeting
    assert 'Skip all self-introduction' in session_config(m.settings, record)['input'][0]['content'][0]['text']
    assert m.result(cid)['presentation']['owner_on_line'] is True


@pytest.mark.parametrize('language', ['en', 'uk', 'es'])
def test_first_person_fallback_language_forwarding_and_reconnect(service, language):
    _, m, _ = service
    contact(m, number='+12025550102')
    m.knowledge.set_language('+12025550102', language, source='owner')
    record = record_for(m, 'inbound', saved(m))
    record['incoming_context']['forwarding']['missed_call_context'] = True
    fallback = startup_greeting_content(m.settings, record)
    assert 'Alex' in fallback and 'assistant' not in fallback and 'Rocky' not in fallback
    text = session_config(m.settings, record)['instructions']
    assert 'without implying that the actual human owner has now answered' in text
    record['transcript'] = [{'speaker':'callee','text':'About my appointment','timestamp':1,'end_timestamp':2}]
    assert startup_greeting_content(m.settings, record) is None
    assert 'Continue' in session_config(m.settings, record)['input'][0]['content'][0]['text']
    assert 'update is not a restart' in session_config(m.settings, record)['instructions']
    assert 'Do not greet or introduce yourself again' in realtime_config(m.settings,
        {**record, 'voice_opening_claimed':True}, {'model':'example', 'voice':'example'})['instructions']


def test_role_migration_is_one_time_and_legacy_rocky_cannot_reset_manual_role(service):
    _, m, _ = service
    cat = m.personalities.catalog(); target = cat['default_personality_id']
    original_ids = {p['personality_id'] for p in cat['personalities']}
    m.context.set_profile(ProfileUpdate(role='Old global office role', context='Public activity fact.'))
    # Recreate the pre-migration state with an old pinned call and original columns.
    record = record_for(m, 'inbound', m.personalities.get(target))
    with m.db.connect() as conn:
        legacy = {k:v for k,v in record['personality'].items() if k not in ('role','presentation_mode','spoken_name','outbound_greeting')}
        conn.execute('UPDATE call_personalities SET snapshot=? WHERE call_id=?', (json.dumps(legacy),record['call_id']))
        conn.execute('DELETE FROM personality_context_migration')
        conn.execute('UPDATE telephone_personalities SET role=?,instructions=instructions||? WHERE personality_id=?', ('', '\n'+LEGACY_IDENTITY_STYLE_LINE, target))
        for column in ('role','presentation_mode','spoken_name','outbound_greeting'):
            conn.execute('ALTER TABLE telephone_personalities DROP COLUMN '+column)
    m.personalities = Personalities(m); m.context.personalities = m.personalities
    assert {p['personality_id'] for p in m.personalities.catalog()['personalities']} == original_ids
    assert all(p['presentation_mode']=='assistant' for p in m.personalities.catalog()['personalities'])
    migrated = m.personalities.get(target)
    assert migrated['role'] == 'Old global office role' and LEGACY_IDENTITY_STYLE_LINE not in migrated['instructions']
    assert m.personalities.for_call(record['call_id'])['presentation_mode'] == 'assistant'
    assert m.personalities.for_call(record['call_id'])['role'] == 'Old global office role'
    assert 'role' not in m.context.for_call(record)['profile']
    friend = saved(m)
    m.personalities.set_default(friend['personality_id'])
    m.personalities.save(PersonalityUpdate(name='Default', role='Manually edited scope', revision=migrated['revision']), target)
    m.context.set_profile(ProfileUpdate(role='Old global office role', context='New fact.'))
    assert m.personalities.get(target)['role'] == 'Manually edited scope'
    m.context.set_profile(ProfileUpdate(role='Explicit legacy role change', context='Another fact.'))
    assert m.personalities.get(target)['role'] == 'Explicit legacy role change'
    assert m.personalities.get(friend['personality_id']) == friend
    assert Personalities(m).get(target) == m.personalities.get(target)


def test_rest_mcp_preview_and_contact_assignment(service):
    c, m, gateway = service
    h = owner_headers(c)
    profile = c.post('/live/personalities', headers=h, json={'name':'Fictional me',
        'presentation_mode':'owner_first_person', 'spoken_name':'Alex', 'role':'Personal correspondence'}).json()
    person = contact(m)
    m.personalities.assign(person, profile['personality_id'])
    assert m.personalities.resolve({'phone_number':'+12025550101','direction':'outbound'})['presentation_mode']=='owner_first_person'
    result = c.post('/live/instructions-preview', headers=h, json={'contact_id':str(person)}).json()
    assert result['presentation']['mode']=='owner_first_person' and result['greeting']
    assert 'role' not in result['owner_context']['profile']
    server, _ = create_mcp(m)
    async def run():
        r = await server.call_tool('set_call_personality', {'personality_id':profile['personality_id'],
            'name':'Updated through MCP','revision':1})
        assert not r.is_error
        r = await server.call_tool('get_call_instructions', {'personality_id':profile['personality_id']})
        assert not r.is_error and 'owner_first_person' in str(r)
    asyncio.run(run())
    assert m.personalities.get(profile['personality_id'])['presentation_mode']=='owner_first_person'
    assert not gateway.calls


def test_outbound_uses_outbound_guidance_and_not_incoming_role_intro(service):
    _, m, _ = service
    record = record_for(m, 'outbound', saved(m, greeting='Welcome to the office.', outbound_greeting='Ask about my appointment.'))
    assert 'Ask about my appointment.' in outbound_opening(m.settings, record)
    text = session_config(m.settings, record)['instructions']
    assert 'Welcome to the office.' not in text
    assert 'Ask about my appointment.' in text
