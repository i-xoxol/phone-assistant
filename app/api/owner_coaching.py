from uuid import UUID

from fastapi import APIRouter,HTTPException,Request,Response,WebSocket

from app.api.twilio_webhooks import validated_form
from app.logging_config import log_event

router=APIRouter(prefix='/twilio/owner-coach')
bridge_router=APIRouter(prefix='/twilio/owner-bridge')


@router.post('/connect')
async def connect(request:Request):
    form=await validated_form(request)
    manager=request.app.state.manager
    if form.get('AccountSid')!=manager.settings.twilio_account_sid:
        raise HTTPException(403,'Twilio account mismatch')
    try:xml=await manager.observer.realtime.connect_twiml(form)
    except (ValueError,KeyError):raise HTTPException(403,'Coach binding mismatch') from None
    except Exception as exc:
        log_event('owner_coach_callback_unavailable',error_type=type(exc).__name__)
        raise HTTPException(503,'Coach unavailable') from None
    return Response(xml,media_type='application/xml')


@router.websocket('/media/{transfer_id}')
async def media(transfer_id:UUID,websocket:WebSocket):
    await websocket.app.state.manager.observer.realtime.media(websocket,str(transfer_id))


@bridge_router.websocket('/{transfer_id}/{side}')
async def bridge_media(transfer_id:UUID,side:str,websocket:WebSocket):
    await websocket.app.state.manager.observer.audio_bridge.media(websocket,str(transfer_id),side)
