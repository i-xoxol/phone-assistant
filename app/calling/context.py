"""Owner-maintained, phone-safe context. No provider credentials or calendar writes."""
from datetime import datetime, timedelta, timezone
import re
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.storage.models import E164


def incoming_caller_metadata(record):
    """Presented caller ID, never proof of identity or a confirmed callback."""
    value = record.get('phone_number', '') if record.get('direction') == 'inbound' else ''
    number = value if isinstance(value, str) and re.fullmatch(E164, value) else ''
    return {'number': number, 'available': bool(number), 'source': 'twilio_caller_id',
            'identity_verified': False}


def utcnow():
    return datetime.now(timezone.utc)


class ProfileUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    role: str=Field(max_length=200)
    context: str=Field(max_length=2000)


class AvailabilitySlot(BaseModel):
    model_config=ConfigDict(extra='forbid')
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode='after')
    def ordered(self):
        if self.end<=self.start or self.end-self.start>timedelta(days=1):
            raise ValueError('A free window must be ordered and no longer than one day')
        return self


class AvailabilityUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    slots: list[AvailabilitySlot]=Field(max_length=40)
    time_zone: str=Field(max_length=100)
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    source: str=Field(min_length=1,max_length=120)

    @field_validator('time_zone')
    @classmethod
    def valid_zone(cls,value):
        try: ZoneInfo(value)
        except (KeyError,ValueError): raise ValueError('Use an IANA time zone') from None
        return value

    @model_validator(mode='after')
    def fresh(self):
        current=utcnow()
        if not current<self.expires_at<=self.observed_at+timedelta(hours=1):
            raise ValueError('Availability must expire within one hour of observation and still be fresh')
        if self.observed_at>current+timedelta(seconds=30):
            raise ValueError('Availability observation cannot be in the future')
        if any(s.end>current+timedelta(days=60) for s in self.slots):
            raise ValueError('Availability horizon is at most 60 days')
        ordered=sorted(self.slots,key=lambda s:s.start)
        if any(b.start<a.end for a,b in zip(ordered,ordered[1:])):
            raise ValueError('Free windows must not overlap')
        self.slots=ordered
        return self


class ExpectedCallUpdate(BaseModel):
    model_config=ConfigDict(extra='forbid')
    context_id: UUID=Field(default_factory=uuid4)
    title: str=Field(max_length=100)
    context: str=Field(min_length=1,max_length=1200)
    phone_number: str=Field(default='',pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    expires_at: AwareDatetime

    @field_validator('expires_at')
    @classmethod
    def bounded(cls,value):
        if not utcnow()<value<=utcnow()+timedelta(days=7):
            raise ValueError('Expected-call context must expire within seven days')
        return value


class ConciergeContext:
    def __init__(self,db):
        self.db=db
        with db.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS concierge_profile (
                    id INTEGER PRIMARY KEY CHECK(id=1), role TEXT NOT NULL,
                    context TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS concierge_availability (
                    id INTEGER PRIMARY KEY CHECK(id=1), observed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, time_zone TEXT NOT NULL, source TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS concierge_free_slots (
                    position INTEGER PRIMARY KEY, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS expected_calls (
                    context_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    title TEXT NOT NULL, context TEXT NOT NULL, phone_number TEXT NOT NULL,
                    expires_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
            ''')

    def set_profile(self,body:ProfileUpdate):
        with self.db.connect() as conn:
            conn.execute('INSERT INTO concierge_profile VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                         'role=excluded.role,context=excluded.context,updated_at=excluded.updated_at',
                         (body.role.strip(),body.context.strip(),utcnow().isoformat()))
        return self.snapshot()

    def set_availability(self,body:AvailabilityUpdate):
        observed=body.observed_at.astimezone(timezone.utc).isoformat()
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old=conn.execute('SELECT observed_at FROM concierge_availability WHERE id=1').fetchone()
            if old and observed<old['observed_at']:
                raise ValueError('Older availability cannot overwrite a newer observation')
            conn.execute('INSERT INTO concierge_availability VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                'observed_at=excluded.observed_at,expires_at=excluded.expires_at,time_zone=excluded.time_zone,source=excluded.source',
                (observed,body.expires_at.astimezone(timezone.utc).isoformat(),body.time_zone,body.source))
            conn.execute('DELETE FROM concierge_free_slots')
            conn.executemany('INSERT INTO concierge_free_slots VALUES (?,?,?)',
                [(i,s.start.astimezone(timezone.utc).isoformat(),s.end.astimezone(timezone.utc).isoformat()) for i,s in enumerate(body.slots)])
        return self.snapshot()

    def expect(self,body:ExpectedCallUpdate):
        stamp=utcnow().isoformat()
        cid=str(body.context_id)
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            exists=conn.execute('SELECT active,expires_at FROM expected_calls WHERE context_id=?',(cid,)).fetchone()
            count=conn.execute('SELECT count(*) FROM expected_calls WHERE active=1 AND expires_at>?',(stamp,)).fetchone()[0]
            if (not exists or not exists['active'] or exists['expires_at']<=stamp) and count>=20:
                raise ValueError('At most twenty active anticipated-call notes')
            conn.execute('INSERT INTO expected_calls VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(context_id) DO UPDATE SET '
                'updated_at=excluded.updated_at,title=excluded.title,context=excluded.context,'
                'phone_number=excluded.phone_number,expires_at=excluded.expires_at,active=1',
                (cid,stamp,stamp,body.title,body.context,body.phone_number,body.expires_at.astimezone(timezone.utc).isoformat()))
        return {'context_id':cid,'status':'active','expires_at':body.expires_at.isoformat()}

    def retire(self,context_id):
        with self.db.connect() as conn:
            found=conn.execute('SELECT 1 FROM expected_calls WHERE context_id=?',(context_id,)).fetchone()
            if not found: raise KeyError(context_id)
            conn.execute('UPDATE expected_calls SET active=0,updated_at=? WHERE context_id=?',(utcnow().isoformat(),context_id))
        return {'context_id':context_id,'status':'retired'}

    def snapshot(self,*,caller_number=None):
        stamp=utcnow().isoformat()
        with self.db.connect() as conn:
            row=conn.execute('SELECT role,context,updated_at FROM concierge_profile WHERE id=1').fetchone()
            profile=dict(row) if row else {'role':'','context':'','updated_at':None}
            row=conn.execute('SELECT observed_at,expires_at,time_zone,source FROM concierge_availability WHERE id=1').fetchone()
            availability=dict(row) if row else None
            if availability:
                availability['fresh']=availability['expires_at']>stamp
                availability['slots']=[{'start':max(stamp,r['starts_at']),'end':r['ends_at']} for r in conn.execute(
                    'SELECT * FROM concierge_free_slots WHERE ends_at>? ORDER BY position LIMIT 40',(stamp,))] if availability['fresh'] else []
            query='SELECT context_id,title,context,phone_number,expires_at FROM expected_calls WHERE active=1 AND expires_at>?'
            args=[stamp]
            if caller_number is not None:
                query+=" AND (phone_number='' OR phone_number=?)"
                args.append(caller_number)
            expected=[dict(r) for r in conn.execute(query+' ORDER BY updated_at DESC LIMIT ?',(*args,20 if caller_number is None else 3))]
        return {'profile':profile,'availability':availability,'expected_calls':expected,
                'disclosure':'Owner approved for incoming callers; caller ID matching is a routing hint, never authentication.',
                'booking':'Free windows and anticipated notes do not prove a booking. Supervisor must complete and verify any booking.'}

    def for_call(self,record):
        if record.get('direction')!='inbound' or record.get('inbound_mode')!='concierge':
            return {}
        return self.snapshot(caller_number=record['phone_number'])

