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
        'This is a live exact check, not a reservation. Reuse request_id for corrections. Read back every returned agreed detail, ask for confirmation, '
        'and wait for the caller. The backend independently checks current consent before any write.',SlotRequest),
    tool('book_appointment','Book only the exact checked draft after its complete spoken readback and an unambiguous caller yes. '
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
Use check_appointment_slot, then read its exact agreed details back and ask for confirmation. No reservation yet.
Say you are checking only after dispatching actual work. Stay interruptible; do not finish the conversation while an operation is pending.
Only book_appointment with verified state=confirmed permits saying the appointment is booked.
Unknown/stale/incomplete coverage, outside hours, changed details, expired authority or an uncertain write require owner review.
If the caller cannot wait, save an UNCONFIRMED request. Do not promise a later booking or retry a write.
Read exact persisted date/time/timezone/duration/arrangement after verified success. Never expose conflict titles or other attendees.
Existing-appointment lookup requires the caller's first and last name. Ambiguous/sensitive/stale records need a non-revealing clarification.
Recognized family may receive only bounded current owner statements and verified free/busy; never private calendar descriptions.
Native supervision remains separate. A local booking operation must be reconciled before a supervisor attempts any calendar write.
'''
