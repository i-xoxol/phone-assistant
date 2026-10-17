from fastapi import APIRouter,Depends,Request
from app.api.calls import authenticated
from app.calling.voices import VoiceChoice

router=APIRouter(prefix='/voice',dependencies=[Depends(authenticated)])


@router.get('')
def settings(request:Request):
    return request.app.state.manager.voices.get()


@router.put('')
def choose(body:VoiceChoice,request:Request):
    return request.app.state.manager.voices.set(body)
