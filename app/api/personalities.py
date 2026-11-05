"""Private owner editing and a read-only view of the actual prompt builders."""
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.calls import authenticated
from app.api.live import owner, write_owner
from app.calling.context import ProfileUpdate, ExpectedCallUpdate, utcnow
from app.calling.knowledge import ContactUpdate, FactUpdate
from app.calling.personalities import PersonalityUpdate, PersonalityAssignment
from app.calling.contact_testing import ContactTestChoice

router = APIRouter()


def checked(function, *args):
    try:
        return function(*args)
    except KeyError as exc:
        raise HTTPException(404, str(exc).strip("'")) from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get('/live/personalities', dependencies=[Depends(owner)])
@router.get('/voice/personalities', dependencies=[Depends(authenticated)])
def catalog(request: Request):
    return request.app.state.manager.personalities.catalog()


@router.post('/live/personalities', dependencies=[Depends(write_owner)])
@router.post('/voice/personalities', dependencies=[Depends(authenticated)])
def create(body: PersonalityUpdate, request: Request):
    return checked(request.app.state.manager.personalities.save, body)


@router.put('/live/personalities/{personality_id}', dependencies=[Depends(write_owner)])
@router.put('/voice/personalities/{personality_id}', dependencies=[Depends(authenticated)])
def update(personality_id: UUID, body: PersonalityUpdate, request: Request):
    return checked(request.app.state.manager.personalities.save, body, personality_id)


class CopyName(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r'.*\S.*')


@router.post('/live/personalities/{personality_id}/duplicate', dependencies=[Depends(write_owner)])
@router.post('/voice/personalities/{personality_id}/duplicate', dependencies=[Depends(authenticated)])
def duplicate(personality_id: UUID, body: CopyName, request: Request):
    return checked(request.app.state.manager.personalities.duplicate, personality_id, body.name)


@router.put('/live/default-personality', dependencies=[Depends(write_owner)])
@router.put('/voice/default-personality', dependencies=[Depends(authenticated)])
def default(body: PersonalityAssignment, request: Request):
    if not body.personality_id:
        raise HTTPException(422, 'Select a default personality')
    return checked(request.app.state.manager.personalities.set_default, body.personality_id)


@router.put('/live/contacts/{contact_id}/personality', dependencies=[Depends(write_owner)])
@router.put('/concierge/contacts/{contact_id}/personality', dependencies=[Depends(authenticated)])
def assignment(contact_id: UUID, body: PersonalityAssignment, request: Request):
    return checked(request.app.state.manager.personalities.assign, contact_id, body.personality_id)


@router.put('/live/contacts/{contact_id}/test-mode', dependencies=[Depends(write_owner)])
@router.put('/concierge/contacts/{contact_id}/test-mode', dependencies=[Depends(authenticated)])
def test_mode(contact_id: UUID, body: ContactTestChoice, request: Request):
    return checked(request.app.state.manager.test_contacts.set, contact_id, body)


class SaveContact(ContactUpdate):
    source_call_id: UUID | None = None


@router.put('/live/contacts', dependencies=[Depends(write_owner)])
async def contact(body: SaveContact, request: Request):
    manager = request.app.state.manager
    if body.source_call_id:
        record = checked(manager.db.get, str(body.source_call_id))
        if (not record['phone_number'] or record['phone_number'] != body.phone_number or
                manager.routing.get(record)['caller_identity_ambiguous']):
            raise HTTPException(409, 'Caller number is withheld or ambiguous. Create a contact from a confirmed number instead.')
    result = checked(manager.knowledge.contact,
                     ContactUpdate(**body.model_dump(exclude={'source_call_id'})), True)
    if body.preferred_language is not None and body.phone_number:
        await manager.caller_language_changed(body.phone_number, body.preferred_language or 'en')
    return result


@router.get('/live/owner-context', dependencies=[Depends(owner)])
def context(request: Request):
    manager = request.app.state.manager
    return {**manager.context.snapshot(for_voice=True), 'knowledge': manager.knowledge.search(owner=True, limit=500)}


class OwnerProfileUpdate(ProfileUpdate):
    expected_updated_at: str | None = Field(default=None, max_length=80)


@router.put('/live/owner-context', dependencies=[Depends(write_owner)])
async def profile(body: OwnerProfileUpdate, request: Request):
    manager = request.app.state.manager
    result = checked(manager.context.set_profile, body, body.expected_updated_at) if 'expected_updated_at' in body.model_fields_set else checked(manager.context.set_profile, body)
    await manager.context_changed()
    return result


@router.put('/live/expected-calls', dependencies=[Depends(write_owner)])
async def expected(body: ExpectedCallUpdate, request: Request):
    manager = request.app.state.manager
    result = checked(manager.context.expect, body)
    await manager.context_changed()
    return result


@router.post('/live/expected-calls/{context_id}/retire', dependencies=[Depends(write_owner)])
async def retire_expected(context_id: UUID, request: Request):
    manager = request.app.state.manager
    result = checked(manager.context.retire, str(context_id))
    await manager.context_changed()
    return result


@router.put('/live/facts', dependencies=[Depends(write_owner)])
async def fact(body: FactUpdate, request: Request):
    manager = request.app.state.manager
    result = checked(manager.knowledge.fact, body)
    await manager.context_changed()
    return result


@router.post('/live/facts/{fact_id}/retire', dependencies=[Depends(write_owner)])
async def retire_fact(fact_id: UUID, request: Request):
    manager = request.app.state.manager
    result = checked(manager.knowledge.retire, str(fact_id))
    await manager.context_changed()
    return result


class Preview(BaseModel):
    call_id: UUID | None = None
    contact_id: UUID | None = None
    personality_id: UUID | None = None


def prompt_preview(manager, body):
    """No pins, provider connections, calendar requests or telephone side effects."""
    from app.calling.prompts import session_config
    from app.calling.presentation import resolve_presentation, outbound_opening
    if body.call_id:
        record = manager.db.get(str(body.call_id))
        record['personality'] = manager.personalities.for_call(record['call_id']) or manager.personalities.resolve(record)
        record['test_mode'] = manager.test_contacts.for_call(record['call_id']) or {'enabled': False, 'contact_id': None, 'instructions': ''}
        record['voice_backend'] = manager.voice_backends.for_call(record['call_id']) or manager.voice_backends.for_record(record)
        record['voice_opening_claimed'] = manager.voice_backends.opening_claimed(record['call_id'])
        record['audio_recording'] = manager.recordings.snapshot(record['call_id'])
        record['owner_transfer'] = manager.transfer.for_call(record['call_id'])
        basis = 'Pinned call personality and voice; context is rebuilt from current safe sources, not a historical prompt recording.'
    else:
        number = ''
        if body.contact_id:
            with manager.db.connect() as c:
                row = c.execute('SELECT phone_number FROM concierge_contacts WHERE contact_id=?', (str(body.contact_id),)).fetchone()
            if not row:
                raise KeyError('Contact not found')
            number = row['phone_number']
        record = {'call_id': str(uuid4()), 'created_at': utcnow().isoformat(), 'phone_number': number,
                  'direction': 'inbound', 'inbound_mode': manager.settings.inbound_mode,
                  'inbound_booking_allowed': manager.settings.inbound_booking_allowed,
                  'max_duration_minutes': manager.settings.inbound_max_duration_minutes,
                  'transcript': [], 'call_updates': [], 'constraints': [], 'objective': '', 'context': '', 'recipient': ''}
        record['personality'] = manager.personalities.resolve(record)
        record['test_mode'] = manager.test_contacts.resolve(record)
        record['voice_backend'] = manager.voice_backends.for_record(record)
        record['audio_recording'] = {'pinned_settings': manager.recordings.preferences.get(), 'policy': {}}
        basis = 'Preview of a new incoming call with current settings; forwarding and caller speech are not simulated.'
    if body.personality_id:
        record['personality'] = manager.personalities.get(body.personality_id)
        basis += ' Selected personality is a preview override only.'
    record['voice_name'] = record['voice_backend']['voice']
    if record.get('direction') == 'inbound':
        record['incoming_context'] = manager.incoming_context(record)
        if record['inbound_mode'] == 'messages' and not manager.hours.state()['office_open']:
            record['closed_announcement'] = manager.hours.closed_announcement()
    config = session_config(manager.settings, record)
    presentation = resolve_presentation(manager.settings, record)
    native = None
    provider = record['voice_backend']['provider']
    if provider == 'elevenlabs':
        from app.calling.elevenlabs_session import realtime_config
        native = realtime_config(manager.settings, record, record['voice_backend'])['instructions']
    elif provider == 'gemini':
        from app.calling.gemini_session import gemini_config
        native = gemini_config(manager.settings, record, record['voice_backend'])['system_instruction']
    backend = config.get('delegation', {}).get('responses', {})
    return {'basis': basis, 'personality': record['personality'], 'presentation': presentation,
            'greeting': config.get('input', []) if record.get('direction') == 'inbound' else outbound_opening(manager.settings, record),
            'test_mode': record['test_mode'], 'answering': record['voice_backend'],
            'voice_instructions': native or config['instructions'], 'planner_instructions': backend.get('instructions', ''),
            'startup_input': config.get('input', []), 'tool_definitions': backend.get('tools', []),
            'context': record.get('incoming_context', {}), 'owner_context': manager.context.snapshot(for_voice=True),
            'policy': 'Edit personality role/presentation, shared facts, contacts and approved information below. Fixed honesty, authorization, '
                      'tool, routing and recording policies are visible here and enforced by the server. '
                      'Dynamic availability expires and is refreshed by the calendar bridge.'}


@router.post('/live/instructions-preview', dependencies=[Depends(write_owner)])
@router.post('/voice/instructions-preview', dependencies=[Depends(authenticated)])
def preview(body: Preview, request: Request):
    return checked(prompt_preview, request.app.state.manager, body)
