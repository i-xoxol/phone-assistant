"""Private rules editor, live context injection, and scoped information approvals."""
from uuid import UUID

from fastapi import APIRouter,Depends,HTTPException,Request
from fastapi.responses import Response
from pydantic import BaseModel,ConfigDict

from app.api.calls import authenticated
from app.api.live import ASSETS,HEADERS,owner,write_owner
from app.calling.rules import RuleSet,ReviewRequest,RulePreview
from app.calling.disclosures import DisclosureRequest
from app.storage.models import CallUpdateRequest

router=APIRouter()


def checked(function,*args):
    try:return function(*args)
    except KeyError as exc:raise HTTPException(404,str(exc).strip("'")) from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


async def awaited(function,*args):
    try:return await function(*args)
    except KeyError as exc:raise HTTPException(404,str(exc).strip("'")) from None
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@router.get('/live/control.js')
def javascript():
    return Response((ASSETS/'control.js').read_text(encoding='utf-8'),media_type='application/javascript',headers=HEADERS)


@router.get('/live/rules',dependencies=[Depends(owner)])
@router.get('/voice/rules',dependencies=[Depends(authenticated)])
def rules(request:Request):return request.app.state.manager.rules.get()


@router.put('/live/rules',dependencies=[Depends(write_owner)])
@router.put('/voice/rules',dependencies=[Depends(authenticated)])
def save_rules(body:RuleSet,request:Request):return checked(request.app.state.manager.rules.save,body)


@router.post('/live/rules/review',dependencies=[Depends(write_owner)])
@router.post('/voice/rules/review',dependencies=[Depends(authenticated)])
async def review_rules(body:ReviewRequest,request:Request):return await awaited(request.app.state.manager.rules.review,body)


@router.post('/live/rules/review/{draft_id}/apply',dependencies=[Depends(write_owner)])
@router.post('/voice/rules/review/{draft_id}/apply',dependencies=[Depends(authenticated)])
def apply_review(draft_id:UUID,request:Request):return checked(request.app.state.manager.rules.apply_review,draft_id)


@router.post('/live/rules/preview',dependencies=[Depends(write_owner)])
@router.post('/voice/rules/preview',dependencies=[Depends(authenticated)])
def preview_rules(body:RulePreview,request:Request):return checked(request.app.state.manager.rules.preview,body)


@router.get('/live/calls/{call_id}/approvals',dependencies=[Depends(owner)])
def approvals(call_id:UUID,request:Request):return checked(request.app.state.manager.disclosures.list,str(call_id))


@router.post('/live/calls/{call_id}/approvals',dependencies=[Depends(write_owner)])
def request_approval(call_id:UUID,body:DisclosureRequest,request:Request):
    return checked(request.app.state.manager.disclosures.request,str(call_id),body)


class Decision(BaseModel):
    model_config=ConfigDict(extra='forbid')
    approve: bool


@router.post('/live/approvals/{approval_id}',dependencies=[Depends(write_owner)])
async def decide(approval_id:UUID,body:Decision,request:Request):
    return await awaited(request.app.state.manager.disclosures.resolve,str(approval_id),body.approve,'owner_portal')


@router.post('/live/calls/{call_id}/updates',dependencies=[Depends(write_owner)])
async def update_call(call_id:UUID,body:CallUpdateRequest,request:Request):
    return await awaited(request.app.state.manager.send_call_update,str(call_id),body)
