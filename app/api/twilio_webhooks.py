from uuid import UUID
import re

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from twilio.twiml.voice_response import VoiceResponse
from twilio.request_validator import RequestValidator

from app.calling.call_manager import normalize_status
from app.calling.twilio_client import stream_twiml
from app.logging_config import log_event
from app.calling.live_session import handle_media
from app.storage.models import E164, TERMINAL

router = APIRouter(prefix='/twilio')


def declined_incoming():
    response = VoiceResponse()
    response.reject(reason='busy')
    return Response(str(response), media_type='application/xml')


@router.post('/incoming')
async def incoming(request: Request):
    form = await validated_form(request)
    manager = request.app.state.manager
    settings = manager.settings
    if (form.get('AccountSid') != settings.twilio_account_sid or
            form.get('To') != settings.twilio_phone_number or form.get('Direction') != 'inbound'):
        raise HTTPException(403, 'Incoming call binding mismatch')
    if not settings.inbound_enabled or settings.missing():
        return declined_incoming()
    sid = str(form.get('CallSid', ''))
    if not re.fullmatch(r'CA[0-9a-fA-F]{32}', sid):
        raise HTTPException(400, 'Invalid call SID')
    caller = str(form.get('From', ''))
    caller = caller if re.fullmatch(E164, caller) else ''
    try:
        cid = manager.db.create_inbound(sid, caller, settings.inbound_max_duration_minutes,
                                        settings.inbound_max_calls_per_hour, settings.inbound_mode,
                                        settings.inbound_booking_allowed)
    except ValueError:
        return declined_incoming()
    record = manager.db.get(cid)
    if record['status'] in TERMINAL:
        response = VoiceResponse()
        response.hangup()
        return Response(str(response), media_type='application/xml')
    # Twilio is still ringing while it fetches these instructions. Calls.update
    # rejects that state (21220). Establish the stream first; handle_media then
    # sets the provider duration cap before connecting any paid Live session.
    log_event('incoming_call_accepted', call_id=cid)
    return Response(stream_twiml(settings, cid), media_type='application/xml')


@router.post('/incoming/status')
async def incoming_status(request: Request):
    form = await validated_form(request)
    manager = request.app.state.manager
    if form.get('AccountSid') != manager.settings.twilio_account_sid:
        raise HTTPException(403, 'Twilio account mismatch')
    record = manager.db.by_twilio_sid(str(form.get('CallSid', '')))
    if not record:
        return Response(status_code=204)
    if record['direction'] != 'inbound' or (form.get('To') and form['To'] != manager.settings.twilio_phone_number):
        raise HTTPException(403, 'Incoming status binding mismatch')
    try:
        state = normalize_status(form['CallStatus'])
        manager.transition(record['call_id'], state,
            sequence=int(form['SequenceNumber']) if form.get('SequenceNumber') else None,
            duration=int(form['CallDuration']) if form.get('CallDuration') else None)
    except (ValueError, KeyError):
        raise HTTPException(400, 'Invalid status event') from None
    if state in TERMINAL:
        manager.capture_inbound_fallback(record['call_id'])
    log_event('incoming_call_status', call_id=record['call_id'], status=state)
    return Response(status_code=204)


@router.websocket('/media/{call_id}')
async def media(websocket: WebSocket, call_id: UUID):
    await handle_media(websocket, str(call_id))


async def validated_form(request: Request):
    settings = request.app.state.settings
    form = await request.form()
    url = settings.public_url(request.url.path)
    if request.url.query:
        url += '?' + request.url.query
    token = settings.twilio_auth_token.get_secret_value()
    if not token or not RequestValidator(token).validate(
            url, form, request.headers.get('x-twilio-signature', '')):
        raise HTTPException(403, 'Invalid Twilio signature')
    return form


def bound_call(manager, call_id: str, form):
    try:
        record = manager.db.get(call_id)
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    if form.get('AccountSid') != manager.settings.twilio_account_sid:
        raise HTTPException(403, 'Twilio account mismatch')
    sid = form.get('CallSid')
    if not sid or (record['twilio_call_sid'] and record['twilio_call_sid'] != sid):
        raise HTTPException(403, 'Twilio call mismatch')
    # A signed callback can arrive before calls.create returns.
    if not record['twilio_call_sid']:
        manager.db.update(call_id, twilio_call_sid=sid)


@router.post('/voice/{call_id}')
async def voice(call_id: UUID, request: Request):
    form = await validated_form(request)
    bound_call(request.app.state.manager, str(call_id), form)
    return Response(stream_twiml(request.app.state.settings, str(call_id)), media_type='application/xml')


@router.post('/status/{call_id}')
async def status(call_id: UUID, request: Request):
    form = await validated_form(request)
    manager = request.app.state.manager
    bound_call(manager, str(call_id), form)
    try:
        state = normalize_status(form['CallStatus'])
        manager.transition(str(call_id), state, sequence=int(form['SequenceNumber']),
                           duration=int(form['CallDuration']) if form.get('CallDuration') else None)
    except (KeyError, ValueError):
        raise HTTPException(400, 'Invalid status event') from None
    log_event('twilio_status', call_id=str(call_id), status=state)
    return Response(status_code=204)


@router.post('/stream-status/{call_id}')
async def stream_status(call_id: UUID, request: Request):
    form = await validated_form(request)
    manager = request.app.state.manager
    cid = str(call_id)
    bound_call(manager, cid, form)
    state, stream_sid = form.get('StreamEvent'), form.get('StreamSid', '')
    if state not in ('stream-started', 'stream-stopped', 'stream-error') or not re.fullmatch(r'MZ[0-9a-zA-Z_]{1,80}', stream_sid):
        raise HTTPException(400, 'Invalid stream event')
    details = {'state':state, 'stream_sid':stream_sid}
    if state == 'stream-error':
        # Twilio's free-text message may contain URLs or other private data.
        # Capture only an error code and an application-owned description.
        match = re.search(r'\b(319\d{2})\b', str(form.get('StreamError', '')))
        code = match.group(1) if match else 'unknown'
        details['error_code'] = code
        bridge = manager.bridges.get(cid)
        expected = (cid in manager.navigation or cid in manager.stopping or
                    (bridge and (bridge.handoff or bridge.end_requested.is_set())))
        superseded = bridge and bridge.stream_sid != stream_sid
        details['expected_disconnect'] = bool(expected or superseded)
        reason = {'31903':'connection broken pipe', '31921':'connection closed',
                  '31901':'connection timeout', '31924':'protocol error'}.get(code, 'stream error')
        if not details['expected_disconnect']:
            manager.db.voice_error(cid, f'Twilio audio stream failed: {reason} ({code})', details)
    manager.db.emit(cid, 'call.media_stream', details)
    log_event('twilio_stream_status', call_id=cid, **details)
    return Response(status_code=204)
