from uuid import UUID
from fastapi import APIRouter,HTTPException,Request,Response
from app.api.twilio_webhooks import validated_form

router=APIRouter(prefix='/twilio/owner-transfer')


@router.post('/{transfer_id}/{action}')
async def transfer_callback(transfer_id:UUID,action:str,request:Request):
    form=await validated_form(request)
    m=request.app.state.manager
    if form.get('AccountSid')!=m.settings.twilio_account_sid:
        raise HTTPException(403,'Twilio account mismatch')
    tid=str(transfer_id)
    try:
        row=m.transfer.get(tid)
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
