"""Versioned bounded tool inputs, independent of provider transport."""
from datetime import timedelta
from uuid import UUID
from pydantic import AwareDatetime,BaseModel,ConfigDict,Field,model_validator
from app.appointments.contracts import Appointment,Interval
from app.appointments.coaching import ScheduleRequest

SCHEMA_VERSION='1.0'

class SlotRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id: UUID | None = None
    first_name: str = Field(min_length=1,max_length=100)
    last_name: str = Field(min_length=1,max_length=100)
    purpose: str = Field(min_length=1,max_length=160)
    start: AwareDatetime
    end: AwareDatetime
    time_zone: str = Field(max_length=80)
    arrangement: str = Field(min_length=1,max_length=160)
    def appointment(self):
        from zoneinfo import ZoneInfo
        zone=ZoneInfo(self.time_zone)
        intent=self.start.astimezone(zone).isoformat()+' / '+self.end.astimezone(zone).isoformat()+' '+self.time_zone
        return Appointment(self.first_name,self.last_name,self.purpose,
            Interval(self.start,self.end,self.time_zone,intent),self.arrangement)
    @model_validator(mode='after')
    def validate_interval(self):self.appointment();return self

class BookRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    draft_id: UUID
    check_id: UUID
    idempotency_key: str=Field(min_length=1,max_length=100)

class AppointmentLookup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    first_name: str=Field(min_length=1,max_length=100)
    last_name: str=Field(min_length=1,max_length=100)

class OperationStatus(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation_id: UUID

class FamilyAvailability(ScheduleRequest):
    view: str=Field(default='free_busy',pattern='^free_busy$')

def tool(name,description,model=None):
    return {'type':'function','name':name,'description':description,
        'parameters':model.model_json_schema() if model else {'type':'object','properties':{},'additionalProperties':False}}

VOICE_TOOLS=[
    tool('get_booking_status','Read/reconcile this call operation; polling never creates a calendar event. '
        'Injection acknowledgment is not spoken confirmation.',OperationStatus),
    tool('check_appointment_slot','Check one exact ordinary office appointment interval. Collect full name, purpose/course, duration and arrangement first. '
        'This is a live exact check, not a reservation. Reuse request_id for corrections. The server queues an exact readback; '
        'do not repeat or paraphrase it. Wait for the caller. The backend independently checks current consent before any write.',SlotRequest),
    tool('book_appointment','Book only the exact checked draft after its complete spoken readback and clear contextual caller agreement. '
        'Yes, okay, sounds good, that works for me, and go ahead can agree; no prescribed phrase is required. '
        'No invitation, cancellation, rescheduling, charges or exception authority. Reuse the idempotency key; uncertain means reconcile, never repeat.',BookRequest),
    tool('get_my_appointment','Minimal confirmation of one ordinary appointment belonging to this caller. Ask first and last name. '
        'No candidate lists, private purpose, other attendees or unrelated calendar search.',AppointmentLookup),
    tool('get_caller_status','Current broad owner-provided status for a uniquely recognized, already-curated family/friend caller. '
        'Never infer location or private reasons from event titles. No new booking authority.'),
    tool('get_family_availability','Read only verified owner free/busy for a uniquely recognized curated family/friend caller. '
        'No private titles or locations, no expanded booking hours.',FamilyAvailability)]

VOICE_POLICY='''
APPOINTMENTS V1: The listed bounded appointment tools are available for ordinary academic appointments only.
get_call_context and request_quick_lookup remain cache/background reads; neither checks a live exact slot.
Collect first and last name, purpose/course, exact date and time zone, duration and arrangement.
Collect only missing details with short questions. Reuse details already supplied; do not restart intake, re-ask the name or require spelling unless unclear.
Use the caller's own purpose and room wording in the draft. For local office appointments use office time; clarify an explicitly different time zone once.
As soon as the required fields are known, dispatch check_appointment_slot. Do not give a full recap or request confirmation before dispatching it.
Use check_appointment_slot. When readback_queued=true, the server sends instructions for one exact spoken readback.
Read the quoted details exactly once, without a preface or paraphrase; ask the included question and wait. Do not add a second readback. No reservation yet.
The server uses a brief date/time when the remaining details are already established, for example "October 23rd, 3:15 p.m. Is that correct?"
Do not add the year or time zone to a server-issued brief confirmation. Clarify genuinely ambiguous dates/zones rather than repeating settled details.
Clear contextual agreement such as yes, okay, sounds good, that works for me, or go ahead is sufficient after the complete readback.
Never demand an explicit booking sentence, repeated yes, or exact wording. Silence, uncertainty, refusal and corrections are not agreement.
After that agreement, call book_appointment immediately using the returned draft_id/check_id and a stable idempotency key; do not ask again whether to book.
Routine local booking uses these tools directly. Do not wait for a supervisor or cached-context lookup before an exact-slot check or consented booking.
When consent_observed=true, the server already observed agreement to the identical complete proposal: book with the returned IDs without another readback or confirmation.
needs_consent means no write was attempted. Follow readback_queued: when true, wait for a new answer after the fresh exact readback.
When false, wait for the complete caller answer or clarify corrections. Respect refusal/withdrawal; do not repeat the old proposal.
Do not escalate solely for a recoverable readback repair.
The server refreshes expired availability with a new real check before writing; it never extends stale evidence or the request deadline.
Say you are checking only after dispatching actual work. Stay interruptible; do not finish the conversation while an operation is pending.
Only book_appointment with verified state=confirmed permits saying the appointment is booked.
Unknown/stale/incomplete coverage, outside hours, changed details, expired authority or an uncertain write require owner review.
If the caller cannot wait, save an UNCONFIRMED request. Do not promise a later booking or retry a write.
Read exact persisted date/time/timezone/duration/arrangement after verified success. Never expose conflict titles or other attendees.
Existing-appointment lookup requires the caller's first and last name. Ambiguous/sensitive/stale records need a non-revealing clarification.
Recognized family may receive only bounded current owner statements and verified free/busy; never private calendar descriptions.
Native supervision remains separate. A local booking operation must be reconciled before a supervisor attempts any calendar write.
'''
