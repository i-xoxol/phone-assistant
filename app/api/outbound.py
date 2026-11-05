"""Owner-only call composer; the same manager and policy as MCP outbound calls."""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.api.calls import authenticated
from app.api.live import ASSETS, HEADERS, owner, write_owner
from app.calling.outbound import OutboundDefaults
from app.storage.models import CallRequest

router=APIRouter()


@router.get('/live/outbound.js')
def javascript():
    return Response((ASSETS/'outbound.js').read_text(encoding='utf-8'),media_type='application/javascript',headers=HEADERS)


@router.get('/live/outbound/settings',dependencies=[Depends(owner)])
@router.get('/voice/outbound-settings',dependencies=[Depends(authenticated)])
def settings(request:Request):
    return request.app.state.manager.outbound.get()


@router.put('/live/outbound/settings',dependencies=[Depends(write_owner)])
@router.put('/voice/outbound-settings',dependencies=[Depends(authenticated)])
async def save(body:OutboundDefaults,request:Request):
    try:return await request.app.state.manager.outbound.save(body)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@router.post('/live/outbound/calls',status_code=201,dependencies=[Depends(write_owner)])
async def place(body:CallRequest,request:Request):
    if not body.request_id:
        raise HTTPException(422,'A request_id is required for a portal call')
    try:return await request.app.state.manager.place_call(body)
    except ValueError as exc:raise HTTPException(409,str(exc)) from None


@router.get('/live/outbound/requests/{request_id}',dependencies=[Depends(owner)])
def submission(request_id:UUID,request:Request):
    try:return request.app.state.manager.outbound.find_request(request_id)
    except KeyError as exc:raise HTTPException(404,str(exc).strip("'")) from None
