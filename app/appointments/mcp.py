"""Optional facade. New appointment grants are explicit and never defaulted into phone.calls."""
from uuid import UUID
from typing import Literal
from pydantic import AwareDatetime
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import ToolAnnotations
from app.appointments.schemas import SlotRequest,BookRequest
from app.appointments.mcp_auth import SCOPES,authorization_metadata


def require(scope):
    token=get_access_token()
    if token is None or scope not in token.scopes:raise PermissionError('Separate appointment authorization is required')

def install(mcp,manager):
    read=ToolAnnotations(readOnlyHint=True,destructiveHint=False,openWorldHint=False)
    write=ToolAnnotations(readOnlyHint=False,destructiveHint=True,openWorldHint=True,idempotentHint=True)
    @mcp.tool(annotations=read,meta=authorization_metadata('get_appointment_permissions'))
    def get_appointment_permissions()->dict:
        """Request/verify the owner's separate appointment read, book and manage permissions.
        Safe setup check: no call, calendar lookup, booking or subscription changes.
        Missing grants trigger OAuth approval on the existing connection; never bypass approval."""
        for scope in SCOPES:require(scope)
        return {'status':'authorized','scopes':SCOPES,'appointment_action_performed':False}

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=True),
        meta={**authorization_metadata('check_appointment_slot'),'requiredScope':'phone.appointments.read'})
    async def check_appointment_slot(call_id:UUID,request:SlotRequest)->dict:
        """Live exact-slot check for this incoming call, with privacy-filtered result. No reservation.
        General cached free windows are not this API. No calendars or authorization flags are caller arguments."""
        require('phone.appointments.read')
        return await manager.appointments.check(str(call_id),request.model_dump(mode='json'))
    @mcp.tool(annotations=write,meta={**authorization_metadata('book_appointment'),'requiredScope':'phone.appointments.book'})
    async def book_appointment(call_id:UUID,request:BookRequest)->dict:
        """Attempt one exact, consented, call-bound ordinary booking; status uncertainty must be reconciled.
        A separate owner grant, enabled write policy, actual spoken readback and current caller agreement are required.
        Never create a duplicate via an external calendar connector. No cancellation/rescheduling or invitations."""
        require('phone.appointments.book')
        return await manager.appointments.book(str(call_id),request.model_dump(mode='json'))
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False,destructiveHint=False,openWorldHint=True),
        meta={**authorization_metadata('get_booking_status'),'requiredScope':'phone.appointments.read'})
    async def get_booking_status(call_id:UUID,operation_id:UUID)->dict:
        """Read/reconcile a specific call-bound operation. Polling never creates an event.
        Confirmation and caller delivery are independent; inspect the existing operation before another action."""
        require('phone.appointments.read')
        return await manager.appointments.status(str(call_id),str(operation_id))
    @mcp.tool(annotations=read,meta={**authorization_metadata('get_my_appointment'),'requiredScope':'phone.appointments.read'})
    async def get_my_appointment(call_id:UUID,first_name:str,last_name:str)->dict:
        """Minimal current appointment details for the caller's stated first and last name.
        No candidates, private purpose, other attendees or arbitrary event search."""
        require('phone.appointments.read')
        return await manager.appointments.own(str(call_id),{'first_name':first_name,'last_name':last_name})

    @mcp.tool(annotations=write,meta={**authorization_metadata('report_call_request_progress'),'requiredScope':'phone.appointments.read'})
    async def report_call_request_progress(request_id:UUID,phase:Literal['received','checking','conflict'],check_id:UUID|None=None)->dict:
        """Record supervisor processing without answering/fulfilling the native request.
        conflict needs a current exact busy check for this request, and pushes a minimal update promptly.
        This is not a booking, caller receipt or a terminal response; existing native subscriptions stay intact."""
        require('phone.appointments.read')
        return await manager.appointments.progress(str(request_id),phase,str(check_id) if check_id else None)

    @mcp.tool(annotations=write,meta={**authorization_metadata('set_caller_relationship'),'requiredScope':'phone.appointments.manage'})
    def set_caller_relationship(contact_id:UUID,relationship:Literal['wife','mother','friend','none'])->dict:
        """Owner-authorized classification of an EXISTING curated contact. Never infer from caller claims.
        Use only an explicit owner statement. Creates no contacts and widens no booking/reach permissions.
        Requires the separate private-status management grant, not ordinary phone.calls."""
        require('phone.appointments.manage');cid=str(contact_id)
        with manager.db.connect() as db:
            contact=db.execute('SELECT contact_id FROM concierge_contacts WHERE contact_id=?',(cid,)).fetchone()
        if not contact:raise ValueError('Use an existing owner-curated contact')
        with manager.appointments.store.connect() as db:db.execute('INSERT INTO appointment_relationships VALUES(?,?,1) '
            'ON CONFLICT(contact_id) DO UPDATE SET relationship=excluded.relationship,owner_established=1',(cid,relationship))
        return {'status':'saved','contact_id':cid,'relationship':relationship}

    @mcp.tool(annotations=write,meta={**authorization_metadata('set_private_call_notice'),'requiredScope':'phone.appointments.manage'})
    def set_private_call_notice(audience_kind:Literal['contact','appointment'],audience_id:UUID,
            kind:Literal['whereabouts','busy_reason','expected_return','delay_minutes'],value:str,
            effective_from:AwareDatetime,expires_at:AwareDatetime,notice_id:UUID|None=None)->dict:
        """Store a CURRENT OWNER-PROVIDED bounded private status/delay with explicit audience and expiry.
        No calendar inference, medical/student/financial facts, invented traffic or promises.
        Contact values: office/teaching/away/unavailable; meeting/teaching/on_a_call/busy; ISO expected return.
        Appointment delay: 1-120 minutes. Never put private appointment records in set_call_fact."""
        require('phone.appointments.manage');aid=str(audience_id);g=manager.appointments
        if audience_kind=='contact':
            with manager.db.connect() as db:exists=db.execute('SELECT 1 FROM concierge_contacts WHERE contact_id=?',(aid,)).fetchone()
        else:
            with g.store.connect() as db:exists=db.execute("SELECT 1 FROM private_appointments WHERE appointment_id=? AND state='current'",(aid,)).fetchone()
        if not exists:raise ValueError('Use an existing private audience')
        nid=g.notices.set(audience_kind,aid,kind,value,effective_from,expires_at,source='owner',now=g.clock(),
            notice_id=str(notice_id) if notice_id else None)
        return {'status':'saved','notice_id':nid}
