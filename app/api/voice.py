from fastapi import APIRouter,Depends,Request,HTTPException
from app.api.calls import authenticated
from app.calling.voices import VoiceChoice
from app.calling.voice_backends import BackendChoice

router=APIRouter(prefix='/voice',dependencies=[Depends(authenticated)])


@router.get('')
def settings(request:Request):
    return request.app.state.manager.voices.get()


@router.put('')
def choose(body:VoiceChoice,request:Request):
    return request.app.state.manager.voices.set(body)


@router.get('/backend')
def backend(request:Request):
    return request.app.state.manager.voice_backends.get()


@router.put('/backend')
def choose_backend(body:BackendChoice,request:Request):
    try: return request.app.state.manager.voice_backends.set(body)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
