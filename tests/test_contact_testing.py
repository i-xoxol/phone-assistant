"""Owner-created test contact behavior without a carrier or model connection."""
import asyncio
import json
from uuid import uuid4
import pytest

from app.api.personalities import Preview, prompt_preview
from app.calling.contact_testing import ContactTestChoice, ContactTesting, DEFAULT_TEST_INSTRUCTIONS
from app.calling.knowledge import ContactUpdate
from app.calling.prompts import session_config
from tests.test_calls import service, create
from tests.test_phone_controls import contact, owner_headers


def enable(m, cid, instructions=''):
    return m.test_contacts.set(cid, ContactTestChoice(enabled=True, instructions=instructions))


def test_migration_is_opt_in_and_preserves_contacts(service):
    _, m, _ = service
    cid = contact(m)
    before = m.knowledge.caller_book()
    restarted = ContactTesting(m)
    assert restarted.get(cid)['enabled'] is False
    assert restarted.get(cid)['default_instructions'] == DEFAULT_TEST_INSTRUCTIONS
    assert m.knowledge.caller_book() == before
    assert not restarted.resolve({'phone_number': '+12025550101'})['enabled']


def test_toggle_and_instructions_persist_without_changing_personality_or_voice(service):
    _, m, _ = service
    cid = contact(m)
    before = (m.personalities.catalog(), m.voice_backends.get())
    enable(m, cid, 'Test short answers and Ukrainian switching.')
    restarted = ContactTesting(m)
    assert restarted.get(cid)['enabled'] and restarted.get(cid)['active_for_number']
    assert 'Ukrainian' in restarted.get(cid)['instructions']
    restarted.set(cid, ContactTestChoice(enabled=False, instructions='Keep this draft.'))
    assert not restarted.get(cid)['enabled']
    assert restarted.get(cid)['instructions'] == 'Keep this draft.'
    assert (m.personalities.catalog(), m.voice_backends.get()) == before


@pytest.mark.parametrize('number', ['', '+12025550102', 'anonymous'])
def test_unknown_or_withheld_number_cannot_inherit_test_mode(service, number):
    _, m, _ = service
    enable(m, contact(m))
    assert not m.test_contacts.resolve({'phone_number': number})['enabled']


def test_duplicate_number_cannot_enable_and_existing_flag_does_not_apply(service):
    _, m, _ = service
    cid = contact(m);enable(m, cid)
    contact(m, name='Other fictional person')
    assert not m.test_contacts.resolve({'phone_number': '+12025550101'})['enabled']
    assert not m.test_contacts.get(cid)['active_for_number']
    with pytest.raises(ValueError, match='unique phone'):
        enable(m, cid)


def test_no_number_and_missing_contact_are_rejected(service):
    _, m, _ = service
    cid = contact(m, number='')
    with pytest.raises(ValueError, match='unique phone'):
        enable(m, cid)
    with pytest.raises(KeyError):
        enable(m, uuid4())


def test_edited_number_requires_explicit_resave(service):
    _, m, _ = service
    cid = contact(m);enable(m, cid)
    m.knowledge.contact(ContactUpdate(contact_id=cid, name='Fictional moved number', phone_number='+12025550102'))
    assert m.test_contacts.get(cid)['enabled'] and not m.test_contacts.get(cid)['active_for_number']
    assert not m.test_contacts.resolve({'phone_number': '+12025550102'})['enabled']
    enable(m, cid)
    assert m.test_contacts.resolve({'phone_number': '+12025550102'})['enabled']
    assert not m.test_contacts.resolve({'phone_number': '+12025550101'})['enabled']


def test_call_pins_test_settings_and_checkbox_changes_only_new_calls(service):
    c, m, _ = service
    cid = contact(m);enable(m, cid, 'Fictional testing note.')
    call = create(c).json()['call_id']
    pinned = m.test_contacts.for_call(call)
    assert pinned['enabled']
    m.test_contacts.set(cid, ContactTestChoice(enabled=False))
    assert m.voice_record(call)['test_mode'] == pinned
    assert ContactTesting(m).for_call(call) == pinned
    m.db.update(call, twilio_call_sid='OLD_FAKE_CALL')
    new = create(c).json()['call_id']
    assert not m.voice_record(new)['test_mode']['enabled']


def test_prompt_adds_test_scope_to_all_adapters_without_adding_tools(service):
    c, m, _ = service
    cid = contact(m)
    call = create(c).json()['call_id']
    record = m.voice_record(call)
    ordinary = session_config(m.settings, record)
    assert 'OWNER-DESIGNATED TEST CONTACT' not in ordinary['instructions']
    record['test_mode'] = {'enabled': True, 'instructions': DEFAULT_TEST_INSTRUCTIONS, 'contact_id': str(cid)}
    tested = session_config(m.settings, record)
    assert tested['instructions'].startswith(ordinary['instructions'])
    assert 'topics beyond office message-taking' in tested['instructions']
    assert 'Never disclose credentials' in tested['instructions']
    assert 'do not assume the caller is the owner' in tested['instructions']
    assert tested['delegation'].get('responses', {}).get('tools') == ordinary['delegation'].get('responses', {}).get('tools')
    assert tested['audio'] == ordinary['audio']
    from app.calling.elevenlabs_session import realtime_config
    from app.calling.gemini_session import gemini_config
    for text in (realtime_config(m.settings, record, {'model': 'eleven_realtime_v1_mini', 'voice': 'test-clone'})['instructions'],
                 gemini_config(m.settings, record, {'model': 'gemini-3.8-live', 'voice': 'Kore'})['system_instruction']):
        assert 'OWNER-DESIGNATED TEST CONTACT' in text and 'Never disclose credentials' in text


def test_forwarding_uses_presented_caller_and_blocks_ambiguous_owner_metadata(service):
    c, m, _ = service
    from tests.test_recordings import incoming, SID
    enable(m, contact(m, number='+12025550102'))
    incoming(c, m, From='+12025550102', ForwardedFrom='+12025550109')
    record = m.db.by_twilio_sid(SID)
    assert m.test_contacts.for_call(record['call_id'])['enabled']
    m.db.transition(record['call_id'], 'completed')
    sid = 'CA'+'6'*32
    incoming(c, m, CallSid=sid, From='+12025550102', ForwardedFrom='+12025550102')
    assert not m.test_contacts.for_call(m.db.by_twilio_sid(sid)['call_id'])['enabled']


def test_owner_api_and_preview_are_private_and_do_not_dial(service):
    c, m, g = service
    cid = contact(m);h = owner_headers(c)
    path = '/live/contacts/'+str(cid)+'/test-mode'
    assert c.put(path, json={'enabled': True}).status_code in (401, 403)
    assert c.put(path, headers=h, json={'enabled': True, 'instructions': 'Try language changes.'}).status_code == 200
    saved = next(r for r in c.get('/live/bootstrap', headers=h).json()['caller_book'] if r['contact_id'] == str(cid))
    assert saved['test_mode']['enabled']
    result = prompt_preview(m, Preview(contact_id=cid))
    assert result['test_mode']['enabled'] and 'Try language changes.' in result['voice_instructions']
    with m.db.connect() as db:
        assert db.execute('SELECT count(*) FROM call_test_modes').fetchone()[0] == 0
    assert c.put(path, headers=h, json={'enabled': True, 'instructions': 'x'*3001}).status_code == 422
    c.cookies.clear();c.headers.pop('Authorization')
    assert c.put('/concierge/contacts/'+str(cid)+'/test-mode', json={'enabled': True}).status_code == 401
    assert not g.calls


def test_mcp_can_inspect_and_change_owner_test_preference(service):
    from app.mcp.server import create_mcp
    _, m, g = service
    cid = contact(m);server, _ = create_mcp(m)
    async def exercise():
        result = await server.call_tool('set_contact_test_mode', {'contact_id': str(cid), 'enabled': True})
        assert not result.is_error
        result = await server.call_tool('get_contact_test_mode', {'contact_id': str(cid)})
        assert json.loads(result.content[0].text)['enabled']
    asyncio.run(exercise())
    assert not g.calls
