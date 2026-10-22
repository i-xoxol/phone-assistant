"""Separate private registry and journal; never a general phone-safe fact store."""
import hashlib,json,sqlite3,time,unicodedata
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from pathlib import Path
from uuid import uuid4
from app.appointments.contracts import Appointment,Interval,CalendarIdentity,EventIdentity


def stamp():return datetime.now(timezone.utc).isoformat()
def name_key(value):return ' '.join(unicodedata.normalize('NFKC',value).casefold().split())
def instant(value):return value.astimezone(timezone.utc).isoformat()
def payload(value):
    return {'first_name':value.first_name,'last_name':value.last_name,'purpose':value.purpose,
        'start':value.interval.start.astimezone(timezone.utc).isoformat(),'end':value.interval.end.astimezone(timezone.utc).isoformat(),
        'zone':value.interval.zone,'local_intent':value.interval.local_intent,'location':value.location}
def payload_hash(value):return hashlib.sha256(json.dumps(payload(value),sort_keys=True).encode()).hexdigest()
def appointment(row):
    return Appointment(row['first_name'],row['last_name'],row['purpose'],Interval(datetime.fromisoformat(row['start']),
        datetime.fromisoformat(row['end']),row['zone'],row.get('local_intent','')),row['location'])
def event_identity(row):
    return EventIdentity(CalendarIdentity(row['provider'],row['tenant'],row['calendar_id']),row['event_id'],row.get('occurrence',''))


class Registry:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS appointment_schema(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO appointment_schema VALUES(1);
            CREATE TABLE IF NOT EXISTS appointment_drafts(
                draft_id TEXT PRIMARY KEY,call_id TEXT NOT NULL,request_id TEXT NOT NULL UNIQUE,
                revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,first_name TEXT NOT NULL,last_name TEXT NOT NULL,
                purpose TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,zone TEXT NOT NULL,local_intent TEXT NOT NULL,
                location TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS appointment_checks(
                check_id TEXT PRIMARY KEY,draft_id TEXT NOT NULL REFERENCES appointment_drafts(draft_id),
                draft_revision INTEGER NOT NULL,policy_version TEXT NOT NULL,decision TEXT NOT NULL,
                reason TEXT NOT NULL,checked_at TEXT NOT NULL,expires_at TEXT NOT NULL,coverage_complete INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS booking_operations(
                operation_id TEXT PRIMARY KEY,draft_id TEXT NOT NULL UNIQUE REFERENCES appointment_drafts(draft_id),
                call_id TEXT NOT NULL,request_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,payload_hash TEXT NOT NULL,
                revision INTEGER NOT NULL,state TEXT NOT NULL,step TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                provider TEXT NOT NULL,tenant TEXT NOT NULL,calendar_id TEXT NOT NULL,event_id TEXT NOT NULL,occurrence TEXT NOT NULL,
                write_attempted INTEGER NOT NULL DEFAULT 0,source_revision TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
                UNIQUE(call_id,idempotency_key),UNIQUE(call_id,payload_hash));
            CREATE TABLE IF NOT EXISTS private_appointments(
                appointment_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,operation_id TEXT UNIQUE,
                provider TEXT NOT NULL,tenant TEXT NOT NULL,calendar_id TEXT NOT NULL,event_id TEXT NOT NULL,occurrence TEXT NOT NULL,
                source_revision TEXT NOT NULL,authoritative_provider TEXT NOT NULL,state TEXT NOT NULL,
                first_name TEXT NOT NULL,last_name TEXT NOT NULL,first_key TEXT NOT NULL,last_key TEXT NOT NULL,
                start TEXT NOT NULL,end TEXT NOT NULL,zone TEXT NOT NULL,location TEXT NOT NULL,
                disclosure TEXT NOT NULL,checked_at TEXT NOT NULL,expires_at TEXT NOT NULL,
                UNIQUE(provider,tenant,calendar_id,event_id,occurrence));
            CREATE TABLE IF NOT EXISTS appointment_aliases(
                appointment_id TEXT NOT NULL REFERENCES private_appointments(appointment_id),
                provider TEXT NOT NULL,tenant TEXT NOT NULL,calendar_id TEXT NOT NULL,event_id TEXT NOT NULL,occurrence TEXT NOT NULL,
                verified INTEGER NOT NULL,PRIMARY KEY(appointment_id,provider,tenant,calendar_id,event_id,occurrence));
            CREATE TABLE IF NOT EXISTS booking_outbox(
                outbox_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL,operation_revision INTEGER NOT NULL,
                kind TEXT NOT NULL,created_at TEXT NOT NULL,delivery_state TEXT NOT NULL DEFAULT 'pending',
                injection_ack_at TEXT,spoken_at TEXT,caller_receipt_at TEXT,error_code TEXT,
                UNIQUE(operation_id,operation_revision,kind));
            CREATE TABLE IF NOT EXISTS appointment_sync(
                provider TEXT NOT NULL,tenant TEXT NOT NULL,calendar_id TEXT NOT NULL,cursor TEXT,
                state TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(provider,tenant,calendar_id));
            CREATE TABLE IF NOT EXISTS appointment_private_notices(
                notice_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,audience_kind TEXT NOT NULL,
                audience_id TEXT NOT NULL,kind TEXT NOT NULL,value TEXT NOT NULL,
                effective_from TEXT NOT NULL,expires_at TEXT NOT NULL,source TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS appointment_relationships(
                contact_id TEXT PRIMARY KEY,relationship TEXT NOT NULL,owner_established INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS appointment_telemetry(
                event_id TEXT PRIMARY KEY,stage TEXT NOT NULL,created_at TEXT NOT NULL,duration_ms REAL NOT NULL,outcome TEXT NOT NULL);
            ''')
            columns={r[1] for r in db.execute('PRAGMA table_info(private_appointments)')}
            if 'series_id' not in columns:db.execute("ALTER TABLE private_appointments ADD COLUMN series_id TEXT NOT NULL DEFAULT ''")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:yield db
        finally:db.close()

    def get(self,table,key,value):
        if (table,key) not in {('appointment_drafts','draft_id'),('appointment_drafts','request_id'),
                ('appointment_checks','check_id'),('booking_operations','operation_id'),('booking_operations','draft_id')}:
            raise ValueError('Unknown registry lookup')
        with self.connect() as db:row=db.execute(f'SELECT * FROM {table} WHERE {key}=?',(value,)).fetchone()
        if not row:raise KeyError(value)
        return dict(row)

    @contextmanager
    def measure(self,stage):
        started=time.perf_counter();outcome='ok'
        try:yield
        except BaseException:
            outcome='unconfirmed';raise
        finally:
            try:
                with self.connect() as db:db.execute('INSERT INTO appointment_telemetry VALUES(?,?,?,?,?)',
                    (str(uuid4()),stage,stamp(),(time.perf_counter()-started)*1000,outcome))
            except (sqlite3.Error,OSError):pass  # Diagnostics never gate human audio or recovery.

    def draft(self,call_id,request_id,value):
        data=payload(value);digest=payload_hash(value)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');old=db.execute('SELECT * FROM appointment_drafts WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['call_id']!=call_id:raise ValueError('Request binding mismatch')
                if old['payload_hash']==digest:return dict(old)
                if db.execute('SELECT 1 FROM booking_operations WHERE draft_id=?',(old['draft_id'],)).fetchone():
                    raise ValueError('Reconcile the existing operation before changing its details')
                fields=','.join(k+'=?' for k in data)
                db.execute('UPDATE appointment_drafts SET '+fields+',payload_hash=?,revision=revision+1 WHERE draft_id=?',
                    (*data.values(),digest,old['draft_id']))
                did=old['draft_id']
            else:
                did=str(uuid4())
                fields=['draft_id','call_id','request_id','revision','payload_hash',*data,'created_at']
                db.execute('INSERT INTO appointment_drafts('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+')',
                    (did,call_id,request_id,1,digest,*data.values(),stamp()))
        return self.get('appointment_drafts','draft_id',did)

    def check(self,draft,policy,decision,reason,checked,expires,complete):
        cid=str(uuid4())
        with self.connect() as db:db.execute('INSERT INTO appointment_checks VALUES(?,?,?,?,?,?,?,?,?)',
            (cid,draft['draft_id'],draft['revision'],policy,decision,reason,checked.isoformat(),expires.isoformat(),int(complete)))
        return self.get('appointment_checks','check_id',cid)

    def intent(self,draft,key,identity):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM booking_operations WHERE draft_id=? OR (call_id=? AND idempotency_key=?)',
                (draft['draft_id'],draft['call_id'],key)).fetchone()
            if old:
                if old['payload_hash']!=draft['payload_hash'] or old['draft_id']!=draft['draft_id']:
                    raise ValueError('Idempotency payload mismatch')
                return dict(old),False
            oid=str(uuid4());now=stamp()
            db.execute('INSERT INTO booking_operations(operation_id,draft_id,call_id,request_id,idempotency_key,payload_hash,'
                'revision,state,step,created_at,updated_at,provider,tenant,calendar_id,event_id,occurrence) '
                'VALUES(?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)',
                (oid,draft['draft_id'],draft['call_id'],draft['request_id'],key,draft['payload_hash'],'checking','intent',now,now,
                 identity.calendar.provider,identity.calendar.tenant,identity.calendar.calendar,identity.event,identity.occurrence))
        return self.get('booking_operations','operation_id',oid),True

    def prior_call_write(self,call_id):
        with self.connect() as db:row=db.execute('SELECT * FROM booking_operations WHERE call_id=? AND write_attempted=1 ORDER BY created_at LIMIT 1',(call_id,)).fetchone()
        return dict(row) if row else None

    def transition(self,oid,state,step,reason='',write_attempted=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE booking_operations SET state=?,step=?,reason=?,updated_at=?,revision=revision+1 '
                +( ',write_attempted=?' if write_attempted is not None else '')+' WHERE operation_id=?',
                (state,step,reason,stamp(),*([int(write_attempted)] if write_attempted is not None else []),oid))
            row=db.execute('SELECT * FROM booking_operations WHERE operation_id=?',(oid,)).fetchone()
            db.execute('INSERT OR IGNORE INTO booking_outbox(outbox_id,operation_id,operation_revision,kind,created_at) VALUES(?,?,?,?,?)',
                (str(uuid4()),oid,row['revision'],'progress' if state in ('checking','committing') else 'result',stamp()))
        return self.get('booking_operations','operation_id',oid)

    def confirmed(self,operation,draft,event,now,expires):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');eid=event.identity;c=eid.calendar
            db.execute('INSERT INTO private_appointments(appointment_id,revision,operation_id,provider,tenant,calendar_id,event_id,occurrence,'
                'source_revision,authoritative_provider,state,first_name,last_name,first_key,last_key,start,end,zone,location,disclosure,checked_at,expires_at) '
                'VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(provider,tenant,calendar_id,event_id,occurrence) DO UPDATE SET '
                'source_revision=excluded.source_revision,state=excluded.state,checked_at=excluded.checked_at,expires_at=excluded.expires_at,revision=revision+1',
                (str(uuid4()),operation['operation_id'],c.provider,c.tenant,c.calendar,eid.event,eid.occurrence,event.revision,c.provider,'current',
                 draft['first_name'],draft['last_name'],name_key(draft['first_name']),name_key(draft['last_name']),
                 instant(event.interval.start),instant(event.interval.end),event.interval.zone,event.location,'ordinary',instant(now),instant(expires)))
            db.execute("UPDATE booking_operations SET state='confirmed',step='registry_persisted',source_revision=?,"
                'revision=revision+1,updated_at=? WHERE operation_id=?',(event.revision,stamp(),operation['operation_id']))
            row=db.execute('SELECT revision FROM booking_operations WHERE operation_id=?',(operation['operation_id'],)).fetchone()
            db.execute('INSERT OR IGNORE INTO booking_outbox(outbox_id,operation_id,operation_revision,kind,created_at) VALUES(?,?,?,?,?)',
                (str(uuid4()),operation['operation_id'],row['revision'],'result',stamp()))

    def own_appointment(self,first,last,now):
        with self.connect() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM private_appointments WHERE first_key=? AND last_key=? '
                "AND state!='cancelled' AND end>?",(name_key(first),name_key(last),now.isoformat()))]
        if not rows:return {'status':'not_found'}
        if len(rows)!=1:return {'status':'ambiguous'}
        row=rows[0]
        if row['state']!='current':return {'status':'stale'}
        if row['disclosure']!='ordinary':return {'status':'needs_owner'}
        if row['expires_at']<=now.isoformat():return {'status':'stale','appointment':row}
        return {'status':'match','appointment':row}

    def tombstone(self,identity,db=None):
        c=identity.calendar
        if db is None:
            with self.connect() as connection:self.tombstone(identity,connection)
            return
        db.execute("UPDATE private_appointments SET state='cancelled',revision=revision+1 WHERE state!='cancelled' AND ("
            '(provider=? AND tenant=? AND calendar_id=? AND (event_id=? OR (?=\'\' AND series_id=?)) AND (?=\'\' OR occurrence=?)) OR appointment_id IN '
            '(SELECT appointment_id FROM appointment_aliases WHERE provider=? AND tenant=? AND calendar_id=? AND event_id=? AND (?=\'\' OR occurrence=?) AND verified=1))',
            (c.provider,c.tenant,c.calendar,identity.event,identity.occurrence,identity.event,identity.occurrence,identity.occurrence,
             c.provider,c.tenant,c.calendar,identity.event,identity.occurrence,identity.occurrence))

    def delivery(self,outbox_id,state):
        columns={'injection_ack':'injection_ack_at','spoken':'spoken_at','caller_receipt':'caller_receipt_at'}
        if state not in columns:raise ValueError('Unknown delivery state')
        with self.connect() as db:
            row=db.execute('SELECT * FROM booking_outbox WHERE outbox_id=?',(outbox_id,)).fetchone()
            if not row:raise KeyError(outbox_id)
            if state=='spoken' and not row['injection_ack_at']:raise ValueError('No injection acknowledgment')
            if state=='caller_receipt' and not row['spoken_at']:raise ValueError('No observed speech')
            db.execute('UPDATE booking_outbox SET '+columns[state]+'=COALESCE('+columns[state]+',?),delivery_state=? WHERE outbox_id=?',
                (stamp(),state,outbox_id))

    def ingest(self,page,identity,now,*,dry_run=True,expected_cursor=None,resync=False):
        """Bounded private index; absent entries never mean cancellation. No provider writes."""
        if not page.complete:raise ValueError('Do not advance an incomplete cursor')
        counts={'eligible':0,'needs_owner':0,'tombstones':0,'dry_run':dry_run}
        eligible=[];tombstones=[];ineligible=[]
        for event in page.events:
            if event.identity.calendar!=identity:raise ValueError('Change account binding mismatch')
            if event.state=='cancelled':
                counts['tombstones']+=1
                tombstones.append(event.identity)
                continue
            if not event.interval or not now<event.interval.end or (event.interval.start-now).days>60:continue
            if (event.disclosure!='ordinary' or not event.revision or not event.participant_first or not event.participant_last
                    or event.kind=='series' or (event.aliases and not event.alias_verified)):
                counts['needs_owner']+=1;ineligible.append(event.identity);continue
            counts['eligible']+=1;eligible.append(event)
        if dry_run:return counts
        # No calendar change is performed here; source and alias identities are private.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT cursor,state FROM appointment_sync WHERE (provider,tenant,calendar_id)=(?,?,?)',
                (identity.provider,identity.tenant,identity.calendar)).fetchone()
            if current and current['cursor']==page.cursor:return {**counts,'duplicate':True}
            if current and current['state']=='needs_resync' and not resync:raise ValueError('Explicit bounded resync is required')
            if (current['cursor'] if current else None)!=expected_cursor:raise ValueError('Sync cursor changed; discard out-of-order delivery')
            for identifier in tombstones:self.tombstone(identifier,db)
            for identifier in ineligible:
                c=identifier.calendar
                db.execute("UPDATE private_appointments SET state='needs_owner',expires_at=? WHERE (provider,tenant,calendar_id)=(?,?,?) AND (event_id=? OR series_id=?)",
                    (now.isoformat(),c.provider,c.tenant,c.calendar,identifier.event,identifier.event))
            for event in eligible:
                c=event.identity.calendar;ids=(event.identity,*event.aliases)
                matches=set()
                for identifier in ids:
                    x=identifier.calendar;args=(x.provider,x.tenant,x.calendar,identifier.event,identifier.occurrence)
                    matches.update(r[0] for r in db.execute('SELECT appointment_id FROM private_appointments WHERE '
                        '(provider,tenant,calendar_id,event_id,occurrence)=(?,?,?,?,?) UNION SELECT appointment_id FROM '
                        'appointment_aliases WHERE (provider,tenant,calendar_id,event_id,occurrence)=(?,?,?,?,?) AND verified=1',args*2))
                if len(matches)>1:
                    counts['needs_owner']+=1
                    for pid in matches:db.execute("UPDATE private_appointments SET state='needs_owner',expires_at=? WHERE appointment_id=?",(now.isoformat(),pid))
                    continue
                pid=next(iter(matches),str(uuid4()))
                values={'appointment_id':pid,'revision':1,'operation_id':None,'provider':c.provider,'tenant':c.tenant,
                    'calendar_id':c.calendar,'event_id':event.identity.event,'occurrence':event.identity.occurrence,
                    'source_revision':event.revision,'authoritative_provider':event.aliases[0].calendar.provider if event.aliases else c.provider,
                    'state':'current','first_name':event.participant_first,'last_name':event.participant_last,
                    'first_key':name_key(event.participant_first),'last_key':name_key(event.participant_last),
                    'start':instant(event.interval.start),'end':instant(event.interval.end),'zone':event.interval.zone,
                    'location':event.location,'disclosure':'ordinary','checked_at':now.isoformat(),
                    'expires_at':(now+timedelta(minutes=5)).isoformat(),'series_id':event.series_id}
                if matches:
                    old=db.execute('SELECT source_revision FROM private_appointments WHERE appointment_id=?',(pid,)).fetchone()
                    update={k:v for k,v in values.items() if k not in ('appointment_id','revision','operation_id')}
                    db.execute('UPDATE private_appointments SET '+','.join(k+'=?' for k in update)+
                        ',revision=revision+1 WHERE appointment_id=?',(*update.values(),pid))
                else:db.execute('INSERT INTO private_appointments('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
                for alias in event.aliases:
                    a=alias.calendar
                    db.execute('INSERT OR REPLACE INTO appointment_aliases VALUES(?,?,?,?,?,?,1)',
                        (pid,a.provider,a.tenant,a.calendar,alias.event,alias.occurrence))
            db.execute('INSERT INTO appointment_sync VALUES(?,?,?,?,?,?) ON CONFLICT(provider,tenant,calendar_id) '
                'DO UPDATE SET cursor=excluded.cursor,state=excluded.state,updated_at=excluded.updated_at',
                (identity.provider,identity.tenant,identity.calendar,page.cursor,'current',now.isoformat()))
        return counts

    def cursor_expired(self,identity,now):
        with self.connect() as db:
            args=(identity.provider,identity.tenant,identity.calendar)
            db.execute("UPDATE private_appointments SET expires_at=?,state='stale' WHERE state!='cancelled' AND (provider,tenant,calendar_id)=(?,?,?)",(now.isoformat(),*args))
            db.execute('INSERT INTO appointment_sync VALUES(?,?,?,NULL,?,?) ON CONFLICT(provider,tenant,calendar_id) '
                "DO UPDATE SET cursor=NULL,state='needs_resync',updated_at=excluded.updated_at",(*args,'needs_resync',now.isoformat()))
