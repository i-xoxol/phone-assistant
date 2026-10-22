"""Vendor-neutral version 1 contract. No credentials, SDKs or event dictionaries."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal, Protocol
from zoneinfo import ZoneInfo

VERSION = '1.0'
UTC = timezone.utc


class ProviderFault(Exception):
    def __init__(self, code: str, retry_after: int | None = None):
        if code not in {'unavailable','unauthorized','rate_limited','unsupported','stale','conflict','uncertain','invalid','cursor_expired'}:
            code = 'unavailable'
        self.code, self.retry_after = code, retry_after
        super().__init__(code)

class WriteNotAuthorized(Exception):
    """Raised by the server guard before a provider transport has started a write."""


@dataclass(frozen=True)
class CalendarIdentity:
    provider: str
    tenant: str
    calendar: str


@dataclass(frozen=True)
class EventIdentity:
    calendar: CalendarIdentity
    event: str
    occurrence: str = ''


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    zone: str
    local_intent: str = ''

    def __post_init__(self):
        ZoneInfo(self.zone)
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError('Use ordered timezone-aware instants')

    def overlaps(self, other):
        return self.start < other.end and other.start < self.end

    def contains(self, other):
        return self.start <= other.start and other.end <= self.end


def local_instant(value: datetime, zone: str, fold: int | None = None) -> datetime:
    """Reject nonexistent local time, and ambiguous time without explicit disambiguation."""
    if value.tzinfo is not None:
        raise ValueError('Local intent must be naive')
    tz=ZoneInfo(zone);valid=[]
    for candidate in (0,1):
        instant=value.replace(tzinfo=tz,fold=candidate)
        if instant.astimezone(UTC).astimezone(tz).replace(tzinfo=None)==value:
            valid.append(instant)
    unique={v.utcoffset() for v in valid}
    if not valid or (len(unique)>1 and fold is None):
        raise ValueError('Resolve nonexistent or ambiguous local time')
    if fold is not None and fold not in (0,1):raise ValueError('Invalid fold')
    return next(v for v in valid if fold is None or v.fold==fold).astimezone(UTC)


@dataclass(frozen=True)
class Capabilities:
    version: str = VERSION
    exact_reads: bool = False
    event_readback: bool = False
    create: bool = False
    deterministic_identity: bool = False
    suppress_invitations: bool = False
    private_events: bool = False
    conditional_writes: bool = False
    change_cursors: bool = False
    webhook_hints: bool = False
    recurrence_expansion: bool = False
    cancel: bool = False
    owner_agenda: bool = False
    guarded_write: bool = False
    availability_search: bool = False
    human_readable_details: bool = False

    def safe_booking(self):
        return self.version.split('.')[0]=='1' and all((self.exact_reads,self.event_readback,self.create,self.deterministic_identity,
                    self.suppress_invitations,self.private_events,self.recurrence_expansion,self.guarded_write))


@dataclass(frozen=True)
class CalendarEvent:
    identity: EventIdentity
    revision: str
    interval: Interval | None
    state: Literal['busy','free','unknown','cancelled'] = 'busy'
    kind: Literal['commitment','availability','series'] = 'commitment'
    aliases: tuple[EventIdentity,...] = ()
    alias_verified: bool = False
    all_day: tuple[date,date] | None = None
    series_id: str = ''
    recurrence: tuple[str,...] = ()
    location: str = ''
    participant_first: str = ''
    participant_last: str = ''
    disclosure: Literal['ordinary','sensitive','unknown'] = 'unknown'
    ownership: str = ''
    # Read only via the private owner agenda capability; never caller availability results.
    owner_label: str = ''
    purpose: str = ''
    booking_details_verified: bool = False


@dataclass(frozen=True)
class Coverage:
    interval: Interval
    checked_at: datetime
    valid_until: datetime
    complete: bool
    events: tuple[CalendarEvent,...] = ()
    reason: str = ''


@dataclass(frozen=True)
class Appointment:
    first_name: str
    last_name: str
    purpose: str
    interval: Interval
    location: str
    category: Literal['ordinary'] = 'ordinary'

    def __post_init__(self):
        if any(not v.strip() or len(v)>160 for v in (self.first_name,self.last_name,self.purpose,self.location)):
            raise ValueError('Collect bounded name, purpose/course and arrangement')
        if self.interval.end-self.interval.start>timedelta(hours=2):
            raise ValueError('Ordinary meeting duration exceeds policy')


@dataclass(frozen=True)
class ChangePage:
    events: tuple[CalendarEvent,...]
    cursor: str
    complete: bool


class CalendarProviderAdapter(Protocol):
    identity: CalendarIdentity
    capabilities: Capabilities
    async def read_interval(self, interval: Interval, exclude: EventIdentity | None = None) -> Coverage: ...
    async def read_availability(self, interval: Interval) -> Coverage: ...
    async def read_event(self, identity: EventIdentity) -> CalendarEvent | None: ...
    async def read_agenda(self, interval: Interval) -> Coverage: ...
    def intended_identity(self, operation_id: str) -> EventIdentity: ...
    async def create(self, identity: EventIdentity, appointment: Appointment, *, authorize: Callable[[],None]) -> None: ...
    async def changes(self, cursor: str | None = None, *, interval: Interval | None = None) -> ChangePage: ...
