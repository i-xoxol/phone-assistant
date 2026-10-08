from uuid import UUID
import re

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket
from twilio.request_validator import RequestValidator

from app.calling.call_manager import normalize_status
from app.calling.twilio_client import stream_twiml
from app.logging_config import log_event
from app.calling.live_session import handle_media

router = APIRouter(prefix='/twilio')


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
