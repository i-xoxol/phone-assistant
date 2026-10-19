from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Request

from app.api.calls import authenticated
from app.calling.context import ProfileUpdate,AvailabilityUpdate,ExpectedCallUpdate
from app.calling.knowledge import ContactUpdate,FactUpdate
from app.calling.languages import OwnerLanguage

router=APIRouter(prefix='/concierge',dependencies=[Depends(authenticated)])


@router.get('/knowledge')
def knowledge(request:Request,query:str=''):
    if len(query)>600: raise HTTPException(422,'Query too long')
    return request.app.state.manager.knowledge.search(query,owner=True,limit=50)


@router.put('/contacts')
async def contact(body:ContactUpdate,request:Request):
    manager=request.app.state.manager
    try: result=manager.knowledge.contact(body)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    if body.preferred_language is not None and body.phone_number:
        await manager.caller_language_changed(body.phone_number,body.preferred_language or 'en')
    return result


@router.put('/caller-language')
async def caller_language(body:OwnerLanguage,request:Request):
    manager=request.app.state.manager
    try: result=manager.knowledge.set_language(body.phone_number,body.language)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    await manager.caller_language_changed(body.phone_number,body.language)
    return result


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
