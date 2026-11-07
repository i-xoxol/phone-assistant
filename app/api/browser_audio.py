"""Owner-only live audio, distinct from carrier and virtual-call transports."""
from uuid import UUID
from fastapi import APIRouter, Request, WebSocket, Depends
from fastapi.responses import Response
from app.api.live import ASSETS, HEADERS, owner
from app.calling.browser_audio import handle_live_audio

router = APIRouter()

@router.get('/live/browser-audio.js')
def javascript():
    return Response((ASSETS / 'browser-audio.js').read_text(encoding='utf-8'),
                    media_type='application/javascript', headers=HEADERS)

@router.get('/live/calls/{call_id}/live-audio', dependencies=[Depends(owner)])
def status(request: Request, call_id: UUID):
    return request.app.state.manager.browser_audio.status(str(call_id))

@router.websocket('/live/calls/{call_id}/live-audio')
async def audio(websocket: WebSocket, call_id: UUID):
    await handle_live_audio(websocket, str(call_id))
