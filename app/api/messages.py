from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.calls import authenticated
from app.storage.models import SupervisorReply

router = APIRouter(prefix='/messages', dependencies=[Depends(authenticated)])


@router.get('')
def list_messages(request: Request, unread_only: bool = False,
                  limit: int = Query(default=50, ge=1, le=100), before: UUID | None = None):
    try:
        return request.app.state.manager.db.list_messages(unread_only=unread_only, limit=limit,
                                                          before=str(before) if before else None)
    except ValueError:
        raise HTTPException(400, 'Invalid message cursor') from None


@router.get('/{message_id}')
def get_message(message_id: UUID, request: Request):
    try:
        return request.app.state.manager.db.message(str(message_id))
    except KeyError:
        raise HTTPException(404, 'Message not found') from None


@router.post('/{message_id}/read')
def mark_message_read(message_id: UUID, request: Request):
    try:
        return request.app.state.manager.db.mark_message_read(str(message_id))
    except KeyError:
        raise HTTPException(404, 'Message not found') from None


supervisor_router = APIRouter(prefix='/call-requests', dependencies=[Depends(authenticated)])


@supervisor_router.get('')
def call_requests(request: Request, call_id: UUID | None = None, pending_only: bool = True):
    return {'requests':request.app.state.manager.db.list_supervisor_requests(
        call_id=str(call_id) if call_id else None, pending_only=pending_only)}


@supervisor_router.post('/{request_id}/respond')
async def respond(request_id: UUID, body: SupervisorReply, request: Request):
    try:
        return await request.app.state.manager.respond_to_supervisor(str(request_id), body)
    except KeyError:
        raise HTTPException(404, 'Request not found') from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
