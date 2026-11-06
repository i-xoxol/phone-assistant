"""Owner-only contact instruction and fact editors."""
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.api.calls import authenticated
from app.api.live import ASSETS, HEADERS, owner, write_owner
from app.api.personalities import checked
from app.calling.contact_guidance import GuidanceUpdate

router = APIRouter()


@router.get('/live/contact-guidance.js')
def javascript():
    return Response((ASSETS / 'contact-guidance.js').read_text(encoding='utf-8'),
        media_type='application/javascript', headers=HEADERS)


@router.get('/live/contacts/{contact_id}/guidance', dependencies=[Depends(owner)])
@router.get('/concierge/contacts/{contact_id}/guidance', dependencies=[Depends(authenticated)])
def read(contact_id: UUID, request: Request):
    return checked(request.app.state.manager.contact_guidance.get, contact_id)


@router.put('/live/contacts/{contact_id}/guidance', dependencies=[Depends(write_owner)])
@router.put('/concierge/contacts/{contact_id}/guidance', dependencies=[Depends(authenticated)])
def save(contact_id: UUID, body: GuidanceUpdate, request: Request):
    return checked(request.app.state.manager.contact_guidance.save, contact_id, body)
