from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Request

from app.api.calls import authenticated
from app.calling.context import ProfileUpdate,AvailabilityUpdate,ExpectedCallUpdate

router=APIRouter(prefix='/concierge',dependencies=[Depends(authenticated)])


@router.get('/context')
def context(request:Request):
    return request.app.state.manager.context.snapshot()


@router.put('/context')
async def profile(body:ProfileUpdate,request:Request):
    manager=request.app.state.manager
    result=manager.context.set_profile(body)
    await manager.context_changed()
    return result


@router.put('/availability')
async def availability(body:AvailabilityUpdate,request:Request):
    manager=request.app.state.manager
    try: result=manager.context.set_availability(body)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    await manager.context_changed()
    return result


@router.put('/expected-calls')
async def expect(body:ExpectedCallUpdate,request:Request):
    manager=request.app.state.manager
    try: result=manager.context.expect(body)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    await manager.context_changed()
    return result


@router.post('/expected-calls/{context_id}/retire')
async def retire(context_id:UUID,request:Request):
    manager=request.app.state.manager
    try: result=manager.context.retire(str(context_id))
    except KeyError: raise HTTPException(404,'Anticipated-call note not found') from None
    await manager.context_changed()
    return result
