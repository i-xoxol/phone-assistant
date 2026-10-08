import asyncio
import html
import json
import secrets
import time
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse, JSONResponse

router = APIRouter()
COOKIE = '__Host-phone-owner'
ASSETS = Path(__file__).resolve().parents[1] / 'web'
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY', 'X-Content-Type-Options': 'nosniff',
    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}


def owner(request: Request):
    session = request.app.state.web_auth.session(request.cookies.get(COOKIE, ''))
    if not session:
        raise HTTPException(401, 'Pair this browser at /live/login')
    return session


def same_origin(request: Request):
    if request.headers.get('origin') != request.app.state.settings.public_base_url:
        raise HTTPException(403, 'Same-origin request required')


def write_owner(request: Request, session=Depends(owner)):
    same_origin(request)
    if not secrets.compare_digest(request.headers.get('x-csrf-token', ''), session['csrf']):
        raise HTTPException(403, 'CSRF token required')
    return session


def login_page(error=''):
    return HTMLResponse('''<!doctype html><html lang="en"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Pair Phone Assistant</title>
    <link rel="stylesheet" href="/live/style.css"><script src="/live/login.js" defer></script></head><body><main class="login">
    <p class="eyebrow">PHONE ASSISTANT</p><h1>Your calls. In view.</h1>
    <p>Pair this browser to watch live transcripts and end a call directly.</p>
    <form method="post" action="/live/login"><label for="code">One-time pairing code</label>
    <input id="code" name="code" required maxlength="32" autocomplete="one-time-code" spellcheck="false">
    <button class="primary" type="submit">Open call monitor</button></form>
    <p class="error" id="login-error" role="status">''' + html.escape(error) + '''</p>
    <p class="muted">Private to you. Codes expire in 15 minutes; this browser stays paired for 7 days.</p>
    </main></body></html>''', headers=HEADERS)


@router.get('/live/login')
def login():
    return login_page()


@router.post('/live/login')
async def pair(request: Request):
    same_origin(request)
    form = await request.form()
    token = request.app.state.web_auth.login(str(form.get('code', '')))
    if not token:
        if 'application/json' in request.headers.get('accept', ''):
            return JSONResponse({'detail': 'Code invalid, expired, or already used. Request a fresh code.'}, status_code=400, headers=HEADERS)
        return login_page('Code invalid, expired, or already used. Request a fresh code.')
    if 'application/json' in request.headers.get('accept', ''):
        response = JSONResponse({'authenticated': True}, headers=HEADERS)
    else:
        session = request.app.state.web_auth.session(token)
        page = (ASSETS / 'index.html').read_text(encoding='utf-8').replace('{{csrf}}', html.escape(session['csrf']))
        response = HTMLResponse(page, headers=HEADERS)
    response.set_cookie(COOKIE, token, max_age=7 * 86400, secure=True, httponly=True, samesite='strict', path='/')
    return response


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
    try:
        return {'cursor': cursor, 'calls': db.recent_calls(), 'call': db.get(str(call_id)) if call_id else None}
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


@router.post('/live/calls/{call_id}/hangup', dependencies=[Depends(write_owner)])
async def hangup(call_id: UUID, request: Request):
    try:
        return await request.app.state.manager.hangup_call(str(call_id))
    except KeyError:
        raise HTTPException(404, 'Call not found') from None
    except Exception:
        raise HTTPException(502, 'Disconnect unconfirmed. Outgoing audio is blocked for this call; retry hang-up or check Twilio.') from None


@router.post('/live/logout', dependencies=[Depends(write_owner)])
def logout(request: Request):
    request.app.state.web_auth.logout(request.cookies.get(COOKIE, ''))
    response = Response(status_code=204, headers=HEADERS)
    response.delete_cookie(COOKIE, path='/', secure=True, httponly=True, samesite='strict')
    return response


@router.post('/live/pairing-code', dependencies=[Depends(write_owner)])
def pairing_code(request: Request):
    return JSONResponse({'code': request.app.state.web_auth.create_code(), 'expires_in_seconds': 900}, headers=HEADERS)
