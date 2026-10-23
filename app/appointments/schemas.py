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
    arrangement: str = Field(min_length=1,max_length=160,description='Specific room/address or how to connect remotely; in person or online alone is incomplete.')
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

class FirstAvailableRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id: UUID | None = None
    first_name: str=Field(min_length=1,max_length=100)
    last_name: str=Field(min_length=1,max_length=100)
    purpose: str=Field(min_length=1,max_length=160)
    duration_minutes: int=Field(ge=1,le=120,strict=True)
    arrangement: str=Field(min_length=1,max_length=160,description='Specific room/address or how to connect remotely; in person or online alone is incomplete.')
    not_before: AwareDatetime | None = None
    not_after: AwareDatetime | None = None

class AppointmentLookup(BaseModel):
    model_config=ConfigDict(extra='forbid')
    first_name: str=Field(min_length=1,max_length=100)
    last_name: str=Field(min_length=1,max_length=100)

class AvailabilityRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    duration_minutes:int=Field(default=15,ge=1,le=120,strict=True)
    not_before:AwareDatetime | None=None
    not_after:AwareDatetime | None=None

class OperationStatus(BaseModel):
    model_config=ConfigDict(extra='forbid')
    operation_id: UUID

class FamilyAvailability(ScheduleRequest):
    view: str=Field(default='free_busy',pattern='^free_busy$')

def tool(name,description,model=None):
    return {'type':'function','name':name,'description':description,
        'parameters':model.model_json_schema() if model else {'type':'object','properties':{},'additionalProperties':False}}

def slot_tool(name,description,model):
    result=tool(name,description,model);result['parameters']['properties'].pop('request_id',None)
    return result

VOICE_TOOLS=[
    tool('get_appointment_availability','Read fourteen days of verified free office-hours blocks. No private event contents. '
        'Use the conversation to choose suitable times from these blocks and propose one concise date/time to the caller. '
        'Default duration is fifteen minutes. Optional bounds reflect the caller’s timing preference. This does not reserve or book.',AvailabilityRequest),
    slot_tool('find_first_available_slot','Find the earliest verified minute-aligned ordinary office appointment in the next fourteen days. '
        'Use for first available, earliest, or any time: no exact date/time is required. Collect only missing full name, purpose, duration and arrangement. '
        'Optional not_before/not_after are only caller-stated constraints within that window; omit them for first available. '
        'The server owns request identity; supply appointment details only. '
        'The server checks real complete calendar coverage and queues one exact proposal; do not repeat it. '
        'Nothing is booked until the caller agrees to that concrete proposal and book_appointment succeeds.',FirstAvailableRequest),
    tool('get_booking_status','Read/reconcile the server-owned booking for THIS call. Takes no arguments or model-selected IDs. '
        'No operation yet means awaiting consent, never a failed booking. Polling never creates an event.'),
    slot_tool('check_appointment_slot','Check one exact ordinary office appointment interval. Collect full name, purpose/course, duration and arrangement first. '
        'This is a live exact check, not a reservation. The server owns request identity and queues one natural time offer; '
        'say the offered time naturally once and listen. Fast Luna interprets the caller’s reply.',SlotRequest),
    tool('book_appointment','Safely resume THIS call’s server-owned pending booking. Takes no arguments; the server owns draft/check IDs and idempotency. '
        'The Luna booking agent also resumes automatically after the time offer and caller agreement. '
        'No prescribed phrase, invitation, cancellation, rescheduling, charge or exception authority. Uncertain means reconcile, never repeat.'),
    tool('get_my_appointment','Read the date/time and location of one existing ordinary appointment belonging to this caller. '
        'Use the current first and last name already stated; ask only missing name components. Call immediately without requiring '
        'the forgotten appointment date/time, purpose, duration or arrangement. Read only verified minimal details; '
        'no candidate lists, private purpose, other attendees, unrelated search or calendar writes.',AppointmentLookup),
    tool('search_appointment_name','Read-only fallback after an exact appointment-name miss. Tell the caller to hold while checking '
        'upcoming appointment records for close spellings, then call this with the caller-supplied name. '
        'The server keeps candidates private and requires the caller\'s exact current name before returning verified details. '
        'It may ask for spelling; it never books or returns candidate names.',AppointmentLookup),
    tool('get_caller_status','Current broad owner-provided status for a uniquely recognized, already-curated family/friend caller. '
        'Never infer location or private reasons from event titles. No new booking authority.'),
    tool('get_family_availability','Read only verified owner free/busy for a uniquely recognized curated family/friend caller. '
        'No private titles or locations, no expanded booking hours.',FamilyAvailability)]

VOICE_POLICY='''
APPOINTMENTS V1: The listed bounded appointment tools are available for ordinary academic appointments only.
get_call_context and request_quick_lookup remain cache/background reads; neither checks a live exact slot.
For an EXISTING appointment, use get_my_appointment as soon as the caller's current first and last name are known.
The owner authorizes VERY RELAXED native name matching: a name mentioned anywhere by the caller is enough, including alternate spellings, similar pronunciation, separate answers, false starts and earlier mentions. False positives are acceptable for this ordinary appointment lookup. Do not demand an exact self-introduction or spelling ritual.
The server can resolve the name privately with fast Luna against upcoming ordinary appointment names, then freshly verify the selected Google event. Let it finish; do not repeat the name while it works. Only speak the returned appointment details, never invent a match or calendar search.
Before asking for spelling, run get_my_appointment with the best name already heard anywhere in the conversation. Let the private resolver handle uncertainty first; ask spelling only if the server explicitly requests it.
Reuse a name already supplied, including separate answers. Do not ask it again or require the date/time the caller is trying to retrieve.
Existing lookup does not require new-booking purpose, duration, location, slot checks or booking confirmation. It never creates an event.
On match, promptly state only the returned date/time and location. Do not ask the caller to supply those details first or escalate a verified match.
If spoken_answer is returned, say that complete sentence once, including the office/location. This is retrieval, so do not ask "Is that correct?" or repeat a booking confirmation. Close politely when the caller is done.
Caller speech/caption lag can leave lookup pending while the server waits for the complete current answer. Pending is not not_found or a failed verification; never say the appointment does not exist based on pending work.
Use the actual returned lookup status. Only not_found means no matching indexed appointment; needs_owner means identity or disclosure could not be established, and stale means fresh calendar verification failed.
needs_name_search means the exact lookup missed. Say once, "Please hold while I check the upcoming appointments for a different spelling," and immediately call search_appointment_name with the supplied name. Stay on the line; do not report a missing appointment.
search_appointment_name privately checks up to 500 supported ordinary appointment names in the next 90 days. It never exposes candidates; no list of calendar contents is available to the voice model.
needs_name_spelling means speech recognition may have transcribed the name incorrectly. Follow missing_fields: if only last_name is missing, spell only that and reuse the supplied first name. Otherwise ask to spell the first name, then the last separately; retry search_appointment_name with the complete current name.
If the server requests clarification, use the caller's ordinary name or spelling, including "C as in Charlie" forms. Similar spellings/pronunciation are acceptable to the private resolver. Never suggest a list of candidate names or invent appointment details yourself.
After a spelling attempt, a remaining exact miss gets one bounded explanation and an offer of owner review; do not repeat the spelling questions or claim the entire calendar has no appointment.
Use missing_fields to ask only the missing name component; never repeat a full-name question after collecting both components. A spoken name followed by letters is one spelling answer, and "Now the last name, please" continues the same clarification.
name_clarification_exhausted means the one spelling cycle is finished without safe verification. Offer owner review once; do not ask for the name again. Your own repeated question does not erase a complete caller-supplied identity.
After receiving the last missing name component, immediately run the lookup and wait for its result. While it is pending, use at most one brief holding phrase; do not fill the wait with more name questions.
Do not claim you checked an appointment without executing get_my_appointment or search_appointment_name and receiving its result. A request to recheck the same supplied name uses these bounded tools; never invent a calendar-wide search or read unrelated event contents.
On incomplete or changed identity, ask only the missing name detail; ambiguity, sensitivity or stale verification needs a brief non-revealing explanation and owner help, not a repeated intake loop.
For a NEW booking, collect first and last name, purpose/course, duration and arrangement.
Use sensible ordinary defaults when the caller is vague: fifteen minutes, their purpose in brief, and the known owner-approved office for an in-person meeting. The owner can clarify minor details later. Do not turn intake into a form or repeat questions about spelling.
An arrangement needs an actual room/address or a specific way to connect. "In person", "online", "Zoom" or "phone" alone is incomplete.
Ask one short location/connection question if missing. An owner-approved office location may be proposed for agreement; never invent a room, link or callback number.
needs_details with missing_fields=[arrangement] means collect only that detail and reuse the rest of intake, including first available. No calendar write has happened.
When the caller says first available, earliest, next available, or any time, do not ask for a specific date or time.
Use get_appointment_availability to read fourteen days of verified free office-hours blocks. Fast Luna uses those blocks and the call context to choose suitable slots; the Live model handles the natural conversation.
For a simple first available request, find_first_available_slot is also available. Preserve caller-stated weekday/date constraints when searching after a conflict.
Choose a concrete slot from the returned free blocks and call check_appointment_slot with the best understood details. Then offer its date/time once. First available alone is not agreement to an unseen time.
If no slot is verified in that bounded window, explain that limit once; do not repeatedly ask the caller to invent a date/time or claim the entire calendar is unavailable.
For a caller who requests a particular date/time, collect the exact date and time zone and use check_appointment_slot.
Collect only missing details with short questions. Reuse details already supplied; do not restart intake, re-ask the name or require spelling unless unclear.
Use the caller's own purpose and room wording in the draft. For local office appointments use office time; clarify an explicitly different time zone once.
For flexible timing, prefer get_appointment_availability so Luna can select from fourteen days of free blocks using the call context. An exact requested time uses check_appointment_slot directly. Do not give a recap or ask to confirm a clear requested date before that check.
With either slot tool, readback_queued=true means one concise time offer is queued for you. Say it naturally once, for example “Monday, October 26 at 10 a.m.—does that work for you?” No full recital or character-perfect wording is needed.
Do not give an extra summary before or after that offer. The caller's name, purpose, duration and office wording do not need formal re-confirmation.
Do not add the year or time zone to a server-issued brief confirmation. Clarify genuinely ambiguous dates/zones rather than repeating settled details.
Fast Luna reads the conversation and interprets agreement. Accept casual/vague positive wording, imperfect transcription, phonetic names and hesitant assent. The calendar handler trusts that decision; it does not police words or require literal quotation.
Never demand an explicit booking sentence, repeated yes, or exact wording. An actual refusal/cancellation stops booking; a clear time correction selects the changed slot. Minor uncertainty or unclear spelling can be clarified by the owner later.
After that agreement, the server resumes booking automatically once caller audio and captions settle. Do not ask again whether to book.
book_appointment and get_booking_status take no arguments in this voice session. The server owns the current proposal, IDs, idempotency and result.
Use book_appointment only to resume current work if needed. A status of awaiting consent/captions is not a calendar failure. Never invent an operation ID.
Routine local booking uses these tools directly. Do not wait for a supervisor or cached-context lookup before an exact-slot check or consented booking.
When consent_observed=true, the server already observed agreement to the identical complete proposal: allow it to finish without another readback or confirmation.
needs_consent means no write was attempted. Follow readback_queued: when true, wait for a new answer after the fresh exact readback.
When false, wait for the complete caller answer or clarify corrections. Respect refusal/withdrawal; do not repeat the old proposal.
Do not escalate solely for a recoverable readback repair.
The server refreshes expired availability with a new real check before writing; it never extends stale evidence or the request deadline.
Say you are checking only after dispatching actual work. Stay interruptible; do not finish the conversation while an operation is pending.
Only book_appointment with verified state=confirmed permits saying the appointment is booked.
Unknown/stale/incomplete coverage, outside hours, changed details, expired authority or an uncertain write require owner review.
If the caller cannot wait, save an UNCONFIRMED request. Do not promise a later booking or retry a write.
Once state=confirmed, say once naturally: “Great, see you Monday at 10 a.m. Confirmed.” Do not repeat all details or ask for another confirmation. Answer follow-ups from the persisted result. Never expose conflict titles or other attendees.
Existing-appointment lookup requires the caller's first and last name. Ambiguous/sensitive/stale records need a non-revealing clarification.
Recognized family may receive only bounded current owner statements and verified free/busy; never private calendar descriptions.
Native supervision remains separate. A local booking operation must be reconciled before a supervisor attempts any calendar write.
'''
