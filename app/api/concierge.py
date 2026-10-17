from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Request

from app.api.calls import authenticated
from app.calling.context import ProfileUpdate,AvailabilityUpdate,ExpectedCallUpdate
from app.calling.knowledge import ContactUpdate,FactUpdate

router=APIRouter(prefix='/concierge',dependencies=[Depends(authenticated)])


@router.get('/knowledge')
def knowledge(request:Request,query:str=''):
    if len(query)>600: raise HTTPException(422,'Query too long')
    return request.app.state.manager.knowledge.search(query,owner=True,limit=50)


@router.put('/contacts')
def contact(body:ContactUpdate,request:Request):
    try: return request.app.state.manager.knowledge.contact(body)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None


@router.put('/facts')
def fact(body:FactUpdate,request:Request):
    try: return request.app.state.manager.knowledge.fact(body)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None


@router.post('/facts/{fact_id}/retire')
def retire_fact(fact_id:UUID,request:Request):
    try: return request.app.state.manager.knowledge.retire(str(fact_id))
    except KeyError: raise HTTPException(404,'Fact not found') from None


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
