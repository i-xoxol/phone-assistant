"""Browser test calls use paired-owner authentication; never public Twilio routes."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import Response

from app.api.live import ASSETS, HEADERS, owner, write_owner
from app.calling.browser_tests import BrowserCall, handle_browser

router = APIRouter()


@router.get('/live/browser-tests/recent-numbers', dependencies=[Depends(owner)])
def recent_numbers(request: Request):
    return request.app.state.manager.browser_tests.recent_numbers()


@router.get('/live/browser-tests.js')
@router.get('/live/browser-audio-worklet.js')
def javascript(request: Request):
    name = request.url.path.rsplit('/', 1)[-1]
    return Response((ASSETS / name).read_text(encoding='utf-8'), media_type='application/javascript', headers=HEADERS)


@router.post('/live/browser-tests', status_code=201)
async def create(body: BrowserCall, request: Request, session=Depends(write_owner)):
    try:
        return request.app.state.manager.browser_tests.create(body, session['csrf'])
    except KeyError:
        raise HTTPException(404, 'Selected personality was not found') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.websocket('/live/browser-tests/{call_id}/audio')
async def audio(websocket: WebSocket, call_id: UUID):
    await handle_browser(websocket, str(call_id))
