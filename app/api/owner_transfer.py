from uuid import UUID
from fastapi import APIRouter,HTTPException,Request,Response,WebSocket
from app.api.twilio_webhooks import validated_form
from twilio.request_validator import RequestValidator

router=APIRouter(prefix='/twilio/owner-transfer')
media_router=APIRouter(prefix='/twilio/owner-media')


@router.get('/{transfer_id}/prompt/{name}.wav')
async def owner_prompt(transfer_id:UUID,name:str,request:Request):
    m=request.app.state.manager
    url=m.settings.public_url(request.url.path)
    if request.url.query:url+='?'+request.url.query
    token=m.settings.twilio_auth_token.get_secret_value()
    if not token or not RequestValidator(token).validate(
            url,{},request.headers.get('x-twilio-signature','')):
        raise HTTPException(403,'Twilio signature required')
    try:audio=m.screening.audio_for(str(transfer_id),name)
    except KeyError:raise HTTPException(404,'Prompt unavailable') from None
    return Response(audio,media_type='audio/wav',headers={'Cache-Control':'private, no-store',
        'X-Content-Type-Options':'nosniff'})


@router.get('/{transfer_id}/audio/{whisper_id}.wav')
async def private_speech(transfer_id:UUID,whisper_id:UUID,request:Request):
    m=request.app.state.manager
    url=m.settings.public_url(request.url.path)
    if request.url.query: url+='?'+request.url.query
    validator=RequestValidator(m.settings.twilio_auth_token.get_secret_value())
    if not validator.validate(url,{},request.headers.get('x-twilio-signature','')):
        raise HTTPException(403,'Twilio signature required')
    try:
        audio=m.observer.audio_for(str(transfer_id),str(whisper_id))
    except KeyError:
        raise HTTPException(404,'Private speech unavailable') from None
    return Response(audio,media_type='audio/wav',headers={'Cache-Control':'private, no-store',
        'X-Content-Type-Options':'nosniff'})


@media_router.websocket('/{transfer_id}/{side}')
async def owner_media(transfer_id:UUID,side:str,websocket:WebSocket):
    from app.calling.transcription import handle_owner_media
    await handle_owner_media(websocket,str(transfer_id),side)


@router.post('/{transfer_id}/{action}')
async def transfer_callback(transfer_id:UUID,action:str,request:Request):
    form=await validated_form(request)
    m=request.app.state.manager
    if form.get('AccountSid')!=m.settings.twilio_account_sid:
        raise HTTPException(403,'Twilio account mismatch')
    tid=str(transfer_id)
    try:
        row=m.transfer.get(tid)
        if row['strategy']=='conference':
            xml=await m.observer.callback(row,action,form)
            return Response(xml,media_type='application/xml')
        if action=='screen':
            xml=await m.transfer.screen(tid,form)
        elif action=='accept':
            xml=await m.transfer.accept(tid,form)
        elif action=='finished':
            xml=m.transfer.finished(tid,form)
        elif action=='leg-status':
            await m.transfer.child(row,form)
            # Progress callbacks can arrive out of order; decisions/outcome take precedence.
            if form.get('CallStatus')=='ringing' and row['status']=='dialing':
                m.transfer.update(tid,status='ringing')
            return Response(status_code=204)
        else:
            raise HTTPException(404,'Unknown transfer endpoint')
    except KeyError:
        raise HTTPException(404,'Owner transfer not found') from None
    except ValueError:
        raise HTTPException(403,'Transfer callback binding or state mismatch') from None
    return Response(xml,media_type='application/xml')
