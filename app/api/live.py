import asyncio
import html
import json
import secrets
import time
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse, JSONResponse
from app.calling.voices import VoiceChoice
from app.calling.transfer import ReachOwner
from app.web_auth import SESSION_SECONDS
from app.calling.languages import OwnerLanguage

router = APIRouter()
COOKIE = '__Host-phone-owner'
ASSETS = Path(__file__).resolve().parents[1] / 'web'
# Normal HTML form POSTs need their real Origin. `no-referrer` makes it "null"
# in browsers; same-origin still withholds referrers from external destinations.
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
    'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; media-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}


def owner(request: Request):
    session = request.app.state.web_auth.session(request.cookies.get(COOKIE, ''))
    if not session:
        raise HTTPException(401, 'Sign in at /live/login')
    return session


def same_origin(request: Request):
    if request.headers.get('origin') != request.app.state.settings.public_base_url:
        raise HTTPException(403, 'Same-origin request required')


def write_owner(request: Request, session=Depends(owner)):
    same_origin(request)
    if not secrets.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
        raise HTTPException(403, 'CSRF token required')
    return session


def login_page(error='', password_enabled=False, status_code=200):
    password_form = '''<form method="post" action="/live/password/login" id="password-login">
    <label for="username">Username</label>
    <input id="username" name="username" value="owner" autocomplete="username" readonly>
    <label for="password">Password</label>
    <input id="password" type="password" name="password" required maxlength="256" autocomplete="current-password">
    <button class="primary" type="submit">Sign in</button></form>''' if password_enabled else '''
    <p>Pair this browser once, then choose <strong>Password</strong> in the monitor to set up reusable sign-in.</p>'''
    return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign in · Phone Assistant</title>
    <link rel="stylesheet" href="/live/style.css"><script src="/live/login.js" defer></script></head><body><main class="login">
    <p class="eyebrow">PHONE ASSISTANT</p><h1>Your calls. In view.</h1>
    <p>Sign in to watch live transcripts, listen to recordings and manage calls.</p>''' + password_form + '''
    <p class="error" id="login-error" role="status">''' + html.escape(error) + '''</p>
    <details class="pairing-login"''' + ('' if password_enabled else ' open') + '''><summary>Use a one-time pairing code</summary>
    <form method="post" action="/live/login" id="pairing-login"><label for="code">One-time pairing code</label>
    <input id="code" name="code" required maxlength="32" autocomplete="one-time-code" spellcheck="false">
    <button class="primary" type="submit">Pair this browser</button></form></details>
    <p class="muted">Private to you. Codes expire in 15 minutes; this browser stays paired for 7 days.</p>
    </main></body></html>''', headers=HEADERS, status_code=status_code)


@router.get('/live/login')
def login(request: Request):
    return login_page(password_enabled=request.app.state.web_auth.password_enabled())


def session_cookie(response, token):
    response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, secure=True,
                        httponly=True, samesite='strict', path='/')
    return response


@router.post('/live/password/login')
async def password_login(request: Request):
    same_origin(request)
    form = await request.form()
    token = await asyncio.to_thread(request.app.state.web_auth.password_login,
        str(form.get('username', '')), str(form.get('password', '')))
    if not token:
        return login_page('Sign-in unsuccessful. Check your details or try again in a minute.',
                          password_enabled=request.app.state.web_auth.password_enabled(), status_code=400)
    return session_cookie(RedirectResponse('/live', status_code=303, headers=HEADERS), token)


@router.post('/live/login')
async def pair(request: Request):
    same_origin(request)
    form = await request.form()
    token = request.app.state.web_auth.login(str(form.get('code', '')))
    if not token:
        if 'application/json' in request.headers.get('accept', ''):
            return JSONResponse({'detail': 'Code invalid, expired, or already used. Request a fresh code.'}, status_code=400, headers=HEADERS)
        return login_page('Code invalid, expired, or already used. Request a fresh code.',
                          password_enabled=request.app.state.web_auth.password_enabled())
    if 'application/json' in request.headers.get('accept', ''):
        response = JSONResponse({'authenticated': True}, headers=HEADERS)
    else:
        session = request.app.state.web_auth.session(token)
        page = (ASSETS / 'index.html').read_text(encoding='utf-8').replace('{{csrf}}', html.escape(session['csrf']))
        response = HTMLResponse(page, headers=HEADERS)
    return session_cookie(response, token)


def password_page(session, enabled, error='', saved=False, status_code=200):
    current = '''<label for="current-password">Current password</label>
    <input id="current-password" name="current_password" type="password" required maxlength="256"
      autocomplete="current-password">''' if enabled else ''
    message = '<p class="success" role="status">Password saved. Other browsers have been signed out. Sign in with username <strong>owner</strong> on your phone or laptop.</p>' if saved else ''
    return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Password · Phone Assistant</title>
    <link rel="stylesheet" href="/live/style.css"></head><body><main class="login">
    <p class="eyebrow">PHONE ASSISTANT · PRIVATE ACCESS</p><h1>''' + ('Change' if enabled else 'Create') + ''' your password</h1>
    <p>Save it in your password manager to sign back in whenever your browser session resets.</p>''' + message + '''
    <form method="post" action="/live/password">
    <input type="hidden" name="csrf" value="''' + html.escape(session['csrf'], quote=True) + '''">
    <label for="username">Username</label><input id="username" name="username" value="owner" autocomplete="username" readonly>
    ''' + current + '''<label for="new-password">New password</label>
    <input id="new-password" name="new_password" type="password" required minlength="12" maxlength="256" autocomplete="new-password">
    <label for="confirm-password">Confirm new password</label>
    <input id="confirm-password" name="confirm_password" type="password" required minlength="12" maxlength="256" autocomplete="new-password">
    <p class="muted">Use 12–256 characters. Saving signs out other browsers and invalidates unused pairing codes.</p>
    <button class="primary" type="submit">Save password</button></form>
    <p class="error" role="status">''' + html.escape(error) + '''</p><p><a href="/live">Back to call monitor</a></p>
    </main></body></html>''', headers=HEADERS, status_code=status_code)


@router.get('/live/password')
def password_settings(request: Request, saved: bool = False):
    try:
        session = owner(request)
    except HTTPException:
        return RedirectResponse('/live/login', status_code=303, headers=HEADERS)
    return password_page(session, request.app.state.web_auth.password_enabled(), saved=saved)


@router.post('/live/password')
async def save_password(request: Request, session=Depends(owner)):
    same_origin(request)
    form = await request.form()
    if not secrets.compare_digest(str(form.get('csrf', '')).encode(), session['csrf'].encode()):
        raise HTTPException(403, 'CSRF token required')
    auth = request.app.state.web_auth
    try:
        if form.get('new_password') != form.get('confirm_password'):
            raise ValueError('The new passwords do not match.')
        token = await asyncio.to_thread(auth.set_password, request.cookies.get(COOKIE, ''),
            str(form.get('current_password', '')), str(form.get('new_password', '')))
    except ValueError as exc:
        return password_page(session, auth.password_enabled(), error=str(exc), status_code=400)
    return session_cookie(RedirectResponse('/live/password?saved=true', status_code=303, headers=HEADERS), token)


@router.get('/live')
def dashboard(request: Request):
    try:
        session = owner(request)
    except HTTPException:
        return RedirectResponse('/live/login', status_code=303, headers=HEADERS)
    page = (ASSETS / 'index.html').read_text(encoding='utf-8').replace('{{csrf}}', html.escape(session['csrf']))
    return HTMLResponse(page, headers=HEADERS)


@router.get('/live/style.css')
def style():
    return Response((ASSETS / 'style.css').read_text(encoding='utf-8'), media_type='text/css', headers=HEADERS)


@router.get('/live/app.js')
def javascript():
    return Response((ASSETS / 'app.js').read_text(encoding='utf-8'), media_type='application/javascript', headers=HEADERS)


@router.get('/live/login.js')
def login_javascript():
    return Response((ASSETS / 'login.js').read_text(encoding='utf-8'), media_type='application/javascript', headers=HEADERS)


@router.get('/live/bootstrap', dependencies=[Depends(owner)])
def bootstrap(request: Request, call_id: UUID | None = None):
    db = request.app.state.manager.db
    # Read head first. Any concurrent arrivals are replayed; source IDs deduplicate.
    cursor = db.event_head()
    saved=request.app.state.manager.context.snapshot()
    availability=saved['availability']
    try:
        return {'cursor': cursor, 'calls': db.recent_calls(), 'call': request.app.state.manager.result(str(call_id)) if call_id else None,
                'caller_book':request.app.state.manager.knowledge.caller_book(),
                'models':{'voice':request.app.state.settings.openai_live_model,
                    'backend':request.app.state.settings.openai_backend_model,
                    'backend_fast':request.app.state.settings.openai_backend_fast,
                    'subscription_lookup_fast':request.app.state.settings.subscription_lookup_fast},
                'voice':request.app.state.manager.voices.get(),
                'owner_contact_enabled':request.app.state.settings.owner_transfer_enabled and bool(request.app.state.settings.owner_transfer_number),
                'inbox':db.list_messages(limit=20),
                'requests':db.list_supervisor_requests(call_id=str(call_id) if call_id else None),
                'concierge':{'enabled':request.app.state.settings.inbound_enabled,
                    'mode':request.app.state.settings.inbound_mode,
                    'role':saved['profile']['role'],
                    'availability_fresh':bool(availability and availability['fresh']),
                    'availability_windows':len(availability['slots']) if availability else 0,
                    'availability_expires_at':availability['expires_at'] if availability else None,
                    'live_supervisor':bool(getattr(request.app.state.manager, 'mcp_events', None) and
                        request.app.state.manager.mcp_events.ready_for('call.supervisor_requested'))}}
    except KeyError:
        raise HTTPException(404, 'Call not found') from None


async def stream_events(manager, cursor: int, expires: float, request=None):
    async with manager.events.subscribe() as ready:
        yield 'event: connected\ndata: {}\n\n'
        while time.time() < expires:
            if request and await request.is_disconnected():
                return
            if request and not request.app.state.web_auth.session(request.cookies.get(COOKIE, '')):
                yield 'event: expired\ndata: {}\n\n'
                return
            ready.clear()  # Clear BEFORE read to avoid losing a wakeup during SQLite replay.
            events = manager.db.events_after(cursor)
            if events:
                for event in events:
                    cursor = event['id']
                    yield f'id: {cursor}\nevent: call_event\ndata: {json.dumps(event, ensure_ascii=False)}\n\n'
                continue
            try:
                await asyncio.wait_for(ready.wait(), timeout=5)
            except TimeoutError:
                yield f'event: heartbeat\ndata: {json.dumps({"server_time": time.time()})}\n\n'
        yield 'event: expired\ndata: {}\n\n'


@router.get('/live/events')
def events(request: Request, cursor: int = 0, session=Depends(owner)):
    try:
        cursor = max(0, int(request.headers.get('last-event-id', cursor)))
    except ValueError:
        raise HTTPException(400, 'Invalid event cursor') from None
    return StreamingResponse(stream_events(request.app.state.manager, cursor, session['expires'], request),
        media_type='text/event-stream', headers={**HEADERS, 'X-Accel-Buffering': 'no'})


@router.put('/live/caller-language',dependencies=[Depends(write_owner)])
async def caller_language(body:OwnerLanguage,request:Request):
    manager=request.app.state.manager
    try: result=manager.knowledge.set_language(body.phone_number,body.language)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    await manager.caller_language_changed(body.phone_number,body.language)
    return JSONResponse(result,headers=HEADERS)


@router.post('/live/calls/{call_id}/hangup', dependencies=[Depends(write_owner)])
async def hangup(call_id: UUID, request: Request):
    try:
        return await request.app.state.manager.hangup_call(str(call_id))
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    except Exception:
        raise HTTPException(502, 'Disconnect unconfirmed. Outgoing audio is blocked for this call; retry hang-up or check Twilio.') from None


@router.put('/live/voice',dependencies=[Depends(write_owner)])
def choose_voice(body:VoiceChoice,request:Request):
    return request.app.state.manager.voices.set(body)


@router.post('/live/calls/{call_id}/reach-owner',dependencies=[Depends(write_owner)])
async def reach_owner(call_id:UUID,body:ReachOwner,request:Request):
    try: return await request.app.state.manager.transfer.request(str(call_id),body.reason,source='owner_monitor')
    except KeyError: raise HTTPException(404,'Call not found') from None
    except ValueError as exc: raise HTTPException(409,str(exc)) from None


@router.post('/live/logout', dependencies=[Depends(write_owner)])
def logout(request: Request):
    request.app.state.web_auth.logout(request.cookies.get(COOKIE, ''))
    response = Response(status_code=204, headers=HEADERS)
    response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
    return response


@router.get('/live/calls/{call_id}/recordings/{recording_id}/audio',dependencies=[Depends(owner)])
async def recording_audio(call_id:UUID,recording_id:str,request:Request):
    from app.api.recordings import audio_response
    return await audio_response(request,call_id,recording_id)


@router.post('/live/calls/{call_id}/recordings/stop',dependencies=[Depends(write_owner)])
async def stop_recording(call_id:UUID,request:Request):
    try: return await request.app.state.manager.recordings.stop(str(call_id))
    except KeyError: raise HTTPException(404,'Call not found') from None


@router.get('/live/messages', dependencies=[Depends(owner)])
def messages(request: Request, limit: int = Query(default=20, ge=1, le=100), before: UUID | None = None):
    try:
        return request.app.state.manager.db.list_messages(limit=limit, before=str(before) if before else None)
    except ValueError:
        raise HTTPException(400, 'Invalid message cursor') from None


@router.post('/live/messages/{message_id}/read', dependencies=[Depends(write_owner)])
def message_read(message_id: UUID, request: Request):
    try:
        return request.app.state.manager.db.mark_message_read(str(message_id))
    except KeyError:
        raise HTTPException(404, 'Message not found') from None


@router.post('/live/pairing-code', dependencies=[Depends(write_owner)])
def pairing_code(request: Request):
    return JSONResponse({'code': request.app.state.web_auth.create_code(), 'expires_in_seconds': 900}, headers=HEADERS)
