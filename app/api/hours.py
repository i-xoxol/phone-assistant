from fastapi import APIRouter,Depends,Request
from app.api.calls import authenticated
from app.calling.hours import HoursSettings

router=APIRouter(prefix='/hours',dependencies=[Depends(authenticated)])

@router.get('')
def get_hours(request:Request):
    return request.app.state.manager.hours.get()

@router.put('')
def set_hours(body:HoursSettings,request:Request):
    return request.app.state.manager.hours.set(body)
