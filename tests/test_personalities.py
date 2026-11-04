"""No real numbers, provider connections, calendar writes or calls."""
import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.api.personalities import Preview, prompt_preview
from app.calling.context import utcnow
from app.calling.personalities import Personalities, PersonalityUpdate
from app.calling.knowledge import ContactUpdate
from app.calling.prompts import session_config, base_session_config
from app.calling.style import STYLE_POLICY
from tests.test_calls import service, create
from tests.test_phone_controls import owner_headers, contact
from tests.test_voice_backends import configure


def profile(manager, name='Fictional friend', **kwargs):
    return manager.personalities.save(PersonalityUpdate(name=name, **kwargs))


def test_seed_preserves_current_default_and_does_not_overwrite_edits(service):
    _, m, _ = service
    catalog = m.personalities.catalog()
    assert {r['name'] for r in catalog['personalities']} == {'Default', 'Office', 'Friend'}
    default = m.personalities.get(catalog['default_personality_id'])
    assert default['instructions'] == STYLE_POLICY.strip()
    default['instructions'] = 'Quiet and precise.'
    m.personalities.save(PersonalityUpdate(**{k: default[k] for k in ('name', 'instructions', 'context', 'greeting', 'revision')}), default['personality_id'])
    restarted = Personalities(m)
    assert restarted.get(default['personality_id'])['instructions'] == 'Quiet and precise.'
    assert len(restarted.catalog()['personalities']) == 3


def test_copy_is_independent_and_stale_editor_cannot_overwrite(service):
    _, m, _ = service
    original = profile(m, instructions='Warm.', context='Fictional public office.', greeting='Be concise.')
    copy = m.personalities.duplicate(original['personality_id'], 'Copy')
    assert copy['personality_id'] != original['personality_id']
    assert [copy[k] for k in ('instructions', 'context', 'greeting')] == [original[k] for k in ('instructions', 'context', 'greeting')]
    m.personalities.save(PersonalityUpdate(name='Copy edited', instructions='Formal.', revision=1), copy['personality_id'])
    assert m.personalities.get(original['personality_id']) == original
    with pytest.raises(ValueError, match='changed elsewhere'):
        m.personalities.save(PersonalityUpdate(name='Stale', revision=1), copy['personality_id'])
    with pytest.raises(ValueError, match='changed elsewhere'):
        m.personalities.save(PersonalityUpdate(name='Missing revision'), copy['personality_id'])


def test_contact_default_ambiguity_and_active_call_snapshot(service):
    c, m, _ = service
    person = contact(m)
    friend = profile(m, instructions='Fictional relaxed style.')
    m.personalities.assign(person, friend['personality_id'])
    cid = create(c).json()['call_id']
    snapshot = m.personalities.for_call(cid)
    assert snapshot == friend
    m.personalities.save(PersonalityUpdate(name='Changed', instructions='Different.', revision=1), friend['personality_id'])
    m.personalities.assign(person, None)
    assert m.voice_record(cid)['personality'] == snapshot
    assert m.personalities.resolve({'phone_number': '+12025550101'})['name'] == 'Default'
    m.personalities.set_default(friend['personality_id'])
    assert m.personalities.resolve({'phone_number': '+12025550101'})['name'] == 'Changed'
    m.personalities.assign(person, snapshot['personality_id'])
    contact(m, name='Ambiguous fictional contact')
    m.personalities.set_default(next(r['personality_id'] for r in m.personalities.catalog()['personalities'] if r['name'] == 'Office'))
    assert m.personalities.resolve({'phone_number': '+12025550101'})['name'] == 'Office'


def test_all_providers_receive_same_personality_and_keep_policy(service):
    c, m, _ = service
    configure(m)
    person = contact(m)
    friend = profile(m, instructions='Be quietly humorous. Do not use thinking sounds.', context='Fictional public reception.', greeting='A quick warm hello.')
    m.personalities.assign(person, friend['personality_id'])
    cid = create(c).json()['call_id']
    record = m.voice_record(cid)
    config = session_config(m.settings, record)
    assert STYLE_POLICY not in config['instructions']
    assert 'Be quietly humorous.' in config['instructions']
    assert 'Fictional public reception.' in config['instructions']
    assert 'If asked' in config['instructions'] and 'AI' in config['instructions']
    from app.calling.elevenlabs_session import realtime_config
    from app.calling.gemini_session import gemini_config
    eleven = realtime_config(m.settings, record, {'model': 'eleven_realtime_v1_mini', 'voice': 'test-clone'})
    gemini = gemini_config(m.settings, record, {'model': 'gemini-3.8-live', 'voice': 'Kore'})
    for text in (eleven['instructions'], gemini['system_instruction']):
        assert 'Be quietly humorous.' in text and 'AUTHOR' in text
    plain = dict(record);plain.pop('personality')
    assert session_config(m.settings, plain) == base_session_config(m.settings, plain)


def test_preview_is_readonly_private_and_uses_pinned_call(service):
    c, m, g = service
    h = owner_headers(c)
    with m.db.connect() as db:
        counts = [db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('calls', 'call_personalities', 'call_voice_backends')]
    response = c.post('/live/instructions-preview', headers=h, json={})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['personality']['name'] == 'Default' and 'FIRST TURN' in data['voice_instructions']
    assert data['tool_definitions'] and data['startup_input']
    assert 'twilio-test' not in response.text and 'local-test' not in response.text
    assert not g.calls
    with m.db.connect() as db:
        assert counts == [db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('calls', 'call_personalities', 'call_voice_backends')]
    cid = create(c).json()['call_id']
    pinned = m.personalities.for_call(cid)
    m.personalities.set_default(profile(m, name='Other')['personality_id'])
    assert prompt_preview(m, Preview(call_id=cid))['personality'] == pinned
    assert len(g.calls) == 1
    assert c.post('/live/instructions-preview', json={}).status_code in (401, 403)
    c.cookies.clear();c.headers.pop('Authorization')
    assert c.get('/live/personalities').status_code == 401


def test_owner_routes_contact_save_dedup_and_assignment(service):
    c, m, g = service
    h = owner_headers(c)
    body = {'name': 'Fictional caller', 'phone_number': '+12025550102', 'preferred_name': 'Pat', 'conversation_style': 'familiar'}
    response = c.put('/live/contacts', headers=h, json=body)
    assert response.status_code == 200
    cid = response.json()['contact_id']
    assert c.put('/live/contacts', headers=h, json=body).status_code == 409
    new = c.post('/live/personalities', headers=h, json={'name': 'Test profile', 'instructions': 'Short replies.'}).json()
    pid = new['personality_id']
    assert c.put('/live/contacts/'+cid+'/personality', headers=h, json={'personality_id': pid}).status_code == 200
    snapshot = c.get('/live/bootstrap', headers=h).json()
    saved = next(r for r in snapshot['caller_book'] if r['contact_id'] == cid)
    assert saved['personality_id'] == pid and saved['preferred_name'] == 'Pat'
    assert saved['conversation_style'] == 'familiar'
    assert c.post('/live/personalities/'+pid+'/duplicate', headers=h, json={'name': 'Copy'}).status_code == 200
    assert c.put('/live/default-personality', headers=h, json={'personality_id': pid}).status_code == 200
    assert c.put('/live/personalities/'+pid, headers=h, json={'name': 'Stale'}).status_code == 409
    assert c.put('/live/contacts/'+cid+'/personality', headers=h, json={'personality_id': str(uuid4())}).status_code == 404
    assert not g.calls


def test_save_from_call_validates_number_and_respects_withheld_id(service):
    c, m, _ = service
    h = owner_headers(c);cid = create(c).json()['call_id']
    body = {'name': 'Fictional recipient', 'phone_number': '+12025550102', 'source_call_id': cid}
    assert c.put('/live/contacts', headers=h, json=body).status_code == 409
    body['phone_number'] = '+12025550101'
    assert c.put('/live/contacts', headers=h, json=body).status_code == 200
    body['phone_number'] = 'withheld'
    assert c.put('/live/contacts', headers=h, json=body).status_code == 422


def test_manual_context_facts_expected_and_retirement(service):
    c, m, _ = service
    h = owner_headers(c)
    assert c.put('/live/owner-context', headers=h, json={'role': 'Fictional receptionist', 'context': 'Public workshop this week.'}).status_code == 200
    note = c.put('/live/expected-calls', headers=h, json={'title': 'Workshop', 'context': 'Ask which workshop.',
        'expires_at': (utcnow()+timedelta(days=1)).isoformat()}).json()
    fact = c.put('/live/facts', headers=h, json={'topic': 'Reception', 'text': 'Public reception is in room Cedar.', 'source': 'Owner',
        'expires_at': (utcnow()+timedelta(days=1)).isoformat(), 'approved_for_callers': True}).json()
    result = c.get('/live/owner-context', headers=h).json()
    assert result['profile']['role'] == 'Fictional receptionist'
    assert result['expected_calls'][0]['context_id'] == note['context_id']
    assert result['knowledge']['facts'][0]['fact_id'] == fact['fact_id']
    assert c.post('/live/facts/'+fact['fact_id']+'/retire', headers=h).status_code == 200
    assert c.post('/live/expected-calls/'+note['context_id']+'/retire', headers=h).status_code == 200
    result = c.get('/live/owner-context', headers=h).json()
    assert not result['expected_calls'] and not result['knowledge']['facts']


def test_owner_context_editor_cannot_overwrite_a_new_rocky_update(service):
    c, m, _ = service
    from app.calling.context import ProfileUpdate
    h = owner_headers(c)
    old = c.get('/live/owner-context', headers=h).json()['profile']['updated_at']
    m.context.set_profile(ProfileUpdate(role='New trusted role', context='Rocky updated this.'))
    result = c.put('/live/owner-context', headers=h, json={'role': 'Old editor', 'context': 'Old text', 'expected_updated_at': old})
    assert result.status_code == 409
    assert m.context.snapshot()['profile']['context'] == 'Rocky updated this.'


def test_incoming_greeting_guidance_is_once_and_forwarding_ambiguity_falls_back(service):
    c, m, _ = service
    from tests.test_recordings import incoming, SID
    person = contact(m, number='+12025550102')
    friend = profile(m, greeting='Use a brief cheerful hello.')
    m.personalities.assign(person, friend['personality_id'])
    incoming(c, m, From='+12025550102')
    record = m.voice_record(m.db.by_twilio_sid(SID)['call_id'])
    config = session_config(m.settings, record)
    assert friend['greeting'] in config['input'][0]['content'][0]['text']
    record['transcript'] = [{'speaker': 'callee', 'text': 'Hello'}]
    config = session_config(m.settings, record)
    assert friend['greeting'] not in config['input'][0]['content'][0]['text']
    # The same number as forwarding source is ambiguous; no contact-specific profile.
    m.db.transition(record['call_id'], 'completed')
    sid = 'CA'+'5'*32
    incoming(c, m, CallSid=sid, From='+12025550102', ForwardedFrom='+12025550102')
    record = m.db.by_twilio_sid(sid)
    assert m.personalities.resolve(record)['name'] == 'Default'


def test_personality_tools_available_through_mcp(service):
    from app.mcp.server import create_mcp
    _, m, g = service
    server, _ = create_mcp(m)
    async def exercise():
        tools = await server.list_tools()
        assert {'get_call_personalities', 'set_call_personality', 'duplicate_call_personality',
                'set_default_call_personality', 'set_contact_personality', 'get_call_instructions'} <= {t.name for t in tools}
        result = await server.call_tool('get_call_personalities', {})
        assert not result.is_error
        result = await server.call_tool('get_call_instructions', {})
        assert not result.is_error
    asyncio.run(exercise())
    assert not g.calls
