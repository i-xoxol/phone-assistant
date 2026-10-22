"""Call-bound facade. Consent evidence is read from storage, never a model-provided flag."""
import asyncio,hashlib,re,sqlite3
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import httpx
from app.appointments.contracts import CalendarIdentity,ProviderFault
from app.appointments.registry import Registry,appointment,name_key
from app.appointments.service import BookingService,CallAuthority,ConsentEvidence
from app.appointments.disclosure import PrivateNotices
from app.appointments.schemas import SlotRequest,BookRequest,AppointmentLookup
from app.storage.models import TERMINAL,CallUpdateRequest


def normalized(text):
    text=text.casefold().replace(':00','').replace('a.m.','am').replace('p.m.','pm')
    for word,number in {'fifteen':'15','thirty':'30','forty-five':'45','sixty':'60','ten':'10','eleven':'11','twelve':'12'}.items():
        text=re.sub(r'\b'+word+r'\b',number,text)
    return re.sub(r'[^\w]','',text)


def readback_text(value):
    from zoneinfo import ZoneInfo
    start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
    minutes=int((value.interval.end-value.interval.start).total_seconds()/60)
    return f'{start.strftime("%B %d %Y at %I:%M %p")} {value.interval.zone}, {minutes} minutes, {value.purpose}, {value.location}. Is that correct?'


def complete_readback(text,value,*,include_identity=True):
    from zoneinfo import ZoneInfo
    start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
    parts=[start.strftime('%B'),str(start.day),str(start.year),start.strftime('%I:%M %p').lstrip('0'),
        str(int((value.interval.end-value.interval.start).total_seconds()/60))+' minutes',value.location]
    if include_identity:parts.extend([value.first_name,value.last_name,value.purpose])
    zone_heard=any(normalized(zone) in normalized(text) for zone in
        (value.interval.zone,start.strftime('%Z'),'Eastern' if value.interval.zone=='America/New_York' else value.interval.zone))
    return zone_heard and all(normalized(part) in normalized(text) for part in parts)


class AppointmentGateway:
    def __init__(self,manager,provider=None,clock=None):
        self.m=manager;self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.store=Registry(manager.settings.appointment_database_path);self.notices=PrivateNotices(self.store)
        self.http=None
        if provider is None:
            from app.appointments.google import GoogleCalendarAdapter,MirrorHealth,OwnerFileToken
            s=manager.settings
            if not s.appointment_google_token_file or not s.appointment_account_namespace:
                raise ValueError('Configure the existing approved Google adapter boundary before enabling appointments')
            self.http=httpx.AsyncClient(follow_redirects=False,trust_env=False)
            token=OwnerFileToken(s.appointment_google_token_file,self.http,self.clock)
            mirror=MirrorHealth(self.http,s.appointment_mirror_status_url,self.clock,
                verified_window=s.appointment_mirror_window_verified)
            provider=GoogleCalendarAdapter(self.http,token,CalendarIdentity('google',s.appointment_account_namespace,
                s.appointment_calendar_id),mirror,self.clock,office_ids=s.appointment_office_event_ids,
                mirror_source=CalendarIdentity('outlook',s.appointment_mirror_source_tenant,s.appointment_mirror_source_calendar)
                    if s.appointment_mirror_source_tenant and s.appointment_mirror_source_calendar else None)
        self.service=BookingService(self.store,provider,self.authority,self.consent,clock=self.clock)
        from app.appointments.sync import IndexSync
        self.sync=IndexSync(self.store,provider,self.clock);self.sync_task=None
        with self.store.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS appointment_requests(
                request_id TEXT PRIMARY KEY,call_id TEXT NOT NULL,deadline TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS appointment_readbacks(
                draft_id TEXT NOT NULL,draft_revision INTEGER NOT NULL,update_id TEXT NOT NULL,created_at TEXT NOT NULL,after_transcript_id INTEGER NOT NULL,
                PRIMARY KEY(draft_id,draft_revision));
                CREATE TABLE IF NOT EXISTS appointment_delivery(operation_id TEXT PRIMARY KEY,update_id TEXT NOT NULL,
                after_transcript_id INTEGER NOT NULL,error_code TEXT);
                CREATE TABLE IF NOT EXISTS appointment_progress(
                update_id TEXT PRIMARY KEY,request_id TEXT NOT NULL,revision INTEGER NOT NULL,phase TEXT NOT NULL,
                created_at TEXT NOT NULL,check_id TEXT,delivery_error TEXT,UNIQUE(request_id,revision));''')

    def transcript(self,call_id):return self.m.db.get(call_id)['transcript']
    def revision(self,call_id):
        turns=[(t.get('source_event_id',''),t.get('timestamp'),t['text']) for t in self.transcript(call_id) if t['speaker']=='callee']
        turns.append(('raw_audio_activity',getattr(self.m,'caller_activity',{}).get(call_id)))
        return hashlib.sha256(repr(turns).encode()).hexdigest()

    def authority(self,call_id,request_id):
        r=self.m.db.get(call_id)
        with self.store.connect() as db:request=db.execute('SELECT * FROM appointment_requests WHERE request_id=?',(request_id,)).fetchone()
        if not request or request['call_id']!=call_id:raise ValueError('Request binding mismatch')
        transfer=self.m.transfer.for_call(call_id)
        available=not transfer or transfer['status'] in ('declined','returned_to_assistant')
        bridge=self.m.bridges.get(call_id)
        active=(r['status']=='in_progress' and r['direction']=='inbound' and r['inbound_mode']=='concierge'
            and call_id not in self.m.stopping and available and not (bridge and (bridge.closing or bridge.handoff or bridge.end_requested.is_set())))
        if any(q['status']=='answered' and q['outcome']=='appointment_confirmed' for q in r.get('supervisor_requests',[])):
            active=False  # Preserve legacy confirmed work, never independently create a second event.
        try:
            native=self.m.db.supervisor_request(request_id)
            active=active and native['status']=='pending' and native['call_id']==call_id
        except KeyError:pass
        return CallAuthority(active,datetime.fromisoformat(request['deadline']),self.revision(call_id),bool(r['inbound_booking_allowed']))

    def request(self,call_id,request_id):
        r=self.m.db.get(call_id);now=self.clock()
        # An existing native supervisor request retains its real server deadline and terminal state.
        native=None
        if request_id:
            request_id=str(request_id)
            with self.store.connect() as db:known=db.execute('SELECT 1 FROM appointment_requests WHERE request_id=? AND call_id=?',(request_id,call_id)).fetchone()
            if known:return request_id
            native=self.m.db.supervisor_request(request_id)
            if native['call_id']!=call_id:raise ValueError('Request binding mismatch')
        else:
            native=next((q for q in r.get('supervisor_requests',[]) if q['status']=='pending' and q['expires_at']>now.isoformat()),None)
        if native:
            if native['status']!='pending' or native['expires_at']<=now.isoformat():raise ValueError('Supervisor request expired or answered')
            with self.store.connect() as db:db.execute('INSERT OR IGNORE INTO appointment_requests VALUES(?,?,?,?)',
                (native['request_id'],call_id,native['expires_at'],now.isoformat()))
            return native['request_id']
        with self.store.connect() as db:old=db.execute('SELECT request_id FROM appointment_requests WHERE call_id=? AND deadline>? ORDER BY created_at DESC LIMIT 1',
            (call_id,now.isoformat())).fetchone()
        if old:return old['request_id']
        began=datetime.fromisoformat(r['started_at'] or r['created_at'])
        deadline=min(now+timedelta(seconds=self.m.settings.supervisor_wait_seconds),began+timedelta(minutes=r['max_duration_minutes']))
        rid=str(uuid4())
        with self.store.connect() as db:db.execute('INSERT INTO appointment_requests VALUES(?,?,?,?)',
            (rid,call_id,deadline.isoformat(),now.isoformat()))
        return rid

    def consent(self,draft,unused):
        value=appointment(draft)
        with self.store.connect() as db:readback=db.execute('SELECT * FROM appointment_readbacks WHERE draft_id=? AND draft_revision=?',
            (draft['draft_id'],draft['revision'])).fetchone()
        accepted=self.m.db.call_update(draft['call_id'],readback['update_id']) if readback else None
        with self.m.db.connect() as db:
            turns=[dict(t) for t in db.execute('SELECT * FROM transcript_events WHERE call_id=? AND id>? ORDER BY start_ms,id',
                (draft['call_id'],readback['after_transcript_id'] if readback else 2**63-1))]
        # Acknowledgment alone is insufficient: require observed assistant readback followed by caller assent.
        heard=False;last_caller=[]
        assistant='';last_speaker=None
        for turn in turns:
            if turn['speaker']=='assistant':
                if last_speaker=='callee':assistant=''
                assistant+=turn['text']
                if complete_readback(assistant,value):heard=True;last_caller=[]
            elif turn['speaker']=='callee' and heard:last_caller.append(turn['text'])
            last_speaker=turn['speaker']
        words=' '.join(''.join(last_caller).casefold().split()).strip(' .!?')
        yes=bool(re.fullmatch(r"yes(?: please)?|yes[,]? (?:that is|that's) correct|correct|that works|так|так[,]? правильно",words))
        # All current caller audio is part of the revision: any later changed detail/withdrawal fails closed.
        activity=getattr(self.m,'caller_activity',{}).get(draft['call_id'])
        caption_end=max((t['end_ms'] for t in turns if t['speaker']=='callee'),default=-1)
        audio_observed=not activity or caption_end>=activity['last_speech_ms']
        return ConsentEvidence(draft['payload_hash'],self.revision(draft['call_id']),
            bool(accepted and accepted['status']=='acknowledged' and heard and audio_observed),yes)

    async def check(self,call_id,args):
        args=SlotRequest.model_validate(args);rid=self.request(call_id,args.request_id)
        draft=self.service.prepare(call_id,rid,args.appointment())
        result=await self.service.check(call_id,draft['draft_id'])
        if result['decision']=='busy':
            try:self.m.db.supervisor_request(rid)
            except KeyError:pass
            else:await self.progress(rid,'conflict',result['check_id'])
        if result['decision']=='available':
            from uuid import UUID,uuid5
            uid=uuid5(UUID(draft['draft_id']),'readback-'+str(draft['revision']))
            value=appointment(draft);text=f'{value.first_name} {value.last_name}, '+readback_text(value)
            if len(text.encode())>330:return {'state':'needs_details','reason':'arrangement_too_long_for_clear_readback'}
            with self.m.db.connect() as db:cursor=db.execute('SELECT coalesce(max(id),0) FROM transcript_events WHERE call_id=?',(call_id,)).fetchone()[0]
            with self.store.connect() as db:db.execute('INSERT OR IGNORE INTO appointment_readbacks VALUES(?,?,?,?,?)',
                (draft['draft_id'],draft['revision'],str(uid),self.clock().isoformat(),cursor))
            await self.m.send_call_update(call_id,CallUpdateRequest(mode='say',content=text,update_id=uid))
        return {**result,'draft_id':draft['draft_id'],'request_id':rid,'schema_version':'1.0',
            'readback':readback_text(appointment(draft)),'readback_queued':result['decision']=='available',
            'instruction':'The readback is queued when available; do not repeat it. Wait for fresh caller agreement after complete readback. Nothing is booked.'}

    async def progress(self,request_id,phase,check_id=None):
        from uuid import UUID,uuid5
        request=self.m.db.supervisor_request(request_id);call_id=request['call_id'];r=self.m.db.get(call_id)
        if (request['status']!='pending' or datetime.fromisoformat(request['expires_at'])<=self.clock()
                or r['status']!='in_progress' or call_id in self.m.stopping):raise ValueError('Request is no longer pending')
        if phase not in ('received','checking','conflict'):raise ValueError('Progress cannot confirm or terminate a request')
        if phase=='conflict':
            check=self.store.get('appointment_checks','check_id',str(check_id))
            draft=self.service.bound(call_id,check['draft_id'])
            if (draft['request_id']!=request_id or check['decision']!='busy' or datetime.fromisoformat(check['expires_at'])<self.clock()):
                raise ValueError('A current verified conflict is required')
        uid=str(uuid5(UUID(request_id),'progress-'+phase+'-'+str(check_id or '')))
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM appointment_progress WHERE update_id=?',(uid,)).fetchone()
            if old:return {**dict(old),'request_terminal':False}
            revision=db.execute('SELECT coalesce(max(revision),0)+1 FROM appointment_progress WHERE request_id=?',(request_id,)).fetchone()[0]
            db.execute('INSERT INTO appointment_progress VALUES(?,?,?,?,?,?,NULL)',
                (uid,request_id,revision,phase,self.clock().isoformat(),check_id))
        if phase=='conflict':
            with self.store.measure('progress_injection'):
                try:await self.m.send_call_update(call_id,CallUpdateRequest(mode='context',update_id=UUID(uid),
                    content="Verified conflict: that time is unavailable. Nothing is booked. The supervisor request is still pending; ask whether another office-hours time works."))
                except (ValueError,RuntimeError):
                    with self.store.connect() as db:db.execute("UPDATE appointment_progress SET delivery_error='call_update_unconfirmed' WHERE update_id=?",(uid,))
        return {'request_id':request_id,'revision':revision,'phase':phase,'request_terminal':False}

    async def book(self,call_id,args):
        args=BookRequest.model_validate(args)
        if not self.m.settings.appointments_writes_enabled:return {'state':'needs_owner','reason':'write_capability_not_enabled'}
        result=await self.service.book(call_id,str(args.draft_id),str(args.check_id),'stored-caller-evidence',args.idempotency_key)
        if result.get('state')=='confirmed':await self.deliver(call_id,result)
        return self.safe_operation(result)

    async def deliver(self,call_id,result):
        from uuid import UUID,uuid5
        with self.store.connect() as db:
            if db.execute('SELECT 1 FROM appointment_delivery WHERE operation_id=?',(result['operation_id'],)).fetchone():return
        with self.m.db.connect() as db:cursor=db.execute('SELECT coalesce(max(id),0) FROM transcript_events WHERE call_id=?',(call_id,)).fetchone()[0]
        uid=uuid5(UUID(result['operation_id']),'verified-booking-result')
        with self.store.connect() as db:db.execute('INSERT INTO appointment_delivery VALUES(?,?,?,NULL)',(result['operation_id'],str(uid),cursor))
        draft=self.store.get('appointment_drafts','draft_id',result['draft_id']);value=appointment(draft)
        text='Calendar and private registry verified: confirmed '+readback_text(value).removesuffix(' Is that correct?')
        try:
            with self.store.measure('result_injection'):await self.m.send_call_update(call_id,CallUpdateRequest(mode='context',content=text,update_id=uid))
        except Exception:
            with self.store.connect() as db:db.execute("UPDATE appointment_delivery SET error_code='call_ended_or_delivery_unconfirmed' WHERE operation_id=?",(result['operation_id'],))

    def delivery_status(self,call_id,oid):
        with self.store.connect() as db:row=db.execute('SELECT * FROM appointment_delivery WHERE operation_id=?',(oid,)).fetchone()
        if not row:return {'injection_accepted':False,'spoken_observed':False,'caller_receipt_observed':False}
        update=self.m.db.call_update(call_id,row['update_id'])
        with self.m.db.connect() as db:turns=[dict(t) for t in db.execute('SELECT * FROM transcript_events WHERE call_id=? AND id>? ORDER BY start_ms,id',
            (call_id,row['after_transcript_id']))]
        said='';spoken=False;receipt=False
        operation=self.store.get('booking_operations','operation_id',oid)
        draft=self.store.get('appointment_drafts','draft_id',operation['draft_id']);value=appointment(draft)
        for turn in turns:
            if turn['speaker']=='assistant':
                said+=turn['text']
                spoken=spoken or ('confirmed' in said.casefold() and complete_readback(said,value,include_identity=False))
            elif spoken and turn['speaker']=='callee' and normalized(turn['text']) in ('yes','correct','thankyou','thanks'):receipt=True
        accepted=bool(update and update['status']=='acknowledged')
        with self.store.connect() as db:
            outbox=db.execute("SELECT outbox_id FROM booking_outbox WHERE operation_id=? AND kind='result' ORDER BY operation_revision DESC LIMIT 1",(oid,)).fetchone()
            if outbox:
                # Independent observations; none is used as authority to repeat calendar creation.
                for present,column in ((accepted,'injection_ack_at'),(spoken,'spoken_at'),(receipt,'caller_receipt_at')):
                    if present:db.execute('UPDATE booking_outbox SET '+column+'=COALESCE('+column+',?) WHERE outbox_id=?',
                        (self.clock().isoformat(),outbox[0]))
                state='caller_receipt' if receipt else 'spoken' if spoken else 'injection_ack' if accepted else 'pending'
                db.execute('UPDATE booking_outbox SET delivery_state=?,error_code=? WHERE outbox_id=?',(state,row['error_code'],outbox[0]))
        return {'injection_accepted':accepted,
            'spoken_observed':spoken,'caller_receipt_observed':receipt,'delivery_error':row['error_code']}

    def safe_operation(self,row):
        result={key:row[key] for key in ('operation_id','state','revision','reason','step') if key in row}
        result['schema_version']='1.0';result['caller_delivery']='unconfirmed'
        if row.get('state')=='confirmed':
            draft=self.store.get('appointment_drafts','draft_id',row['draft_id'])
            result['confirmed_details']={key:draft[key] for key in ('start','end','zone','location')}
            result['confirmed_details']['duration_seconds']=int((datetime.fromisoformat(draft['end'])-datetime.fromisoformat(draft['start'])).total_seconds())
            result['confirmed_details']['participant']={key:draft[key] for key in ('first_name','last_name')}
            result['confirmed_details']['invitations_sent']=False
            result['source_identity']={key:row[key] for key in ('provider','tenant','calendar_id','event_id','occurrence','source_revision')}
        return result

    async def status(self,call_id,oid):
        row=await self.service.status(call_id,oid)
        if row.get('state')=='confirmed':await self.deliver(call_id,row)
        result=self.safe_operation(row)
        result['delivery']=self.delivery_status(call_id,oid)
        return result

    async def own(self,call_id,args):
        args=AppointmentLookup.model_validate(args)
        turns=self.transcript(call_id);caller=' '.join(t['text'] for t in turns if t['speaker']=='callee')
        full=normalized(args.first_name+args.last_name);given=False;name_question=False
        for turn in turns:
            if turn['speaker']=='assistant':
                name_question=bool(re.search(r'full name|first and last name|your name|who am i speaking',turn['text'],re.I))
            elif turn['speaker']=='callee':
                answer=normalized(turn['text'])
                given=given or (name_question and answer in (full,'its'+full,'mynameis'+full,'iam'+full,'im'+full))
                given=given or any(prefix+full in answer for prefix in ('mynameis','thisis','iam','im'))
                name_question=False
        contradictory=not given or bool(re.search(r"\b(?:not|isn't|is not|не)\s+"+re.escape(args.first_name),caller,re.I))
        result=await self.service.own_appointment(args.first_name,args.last_name,contradictory=contradictory)
        if result['status']=='match':
            row=self.store.own_appointment(args.first_name,args.last_name,self.clock()).get('appointment')
            if row:
                with self.store.connect() as db:result['current_delay_notices']=[dict(r) for r in db.execute(
                    "SELECT kind,value,revision FROM appointment_private_notices WHERE audience_kind='appointment' AND audience_id=? "
                    "AND source='owner' AND kind='delay_minutes' AND effective_from<=? AND expires_at>?",
                    (row['appointment_id'],self.clock().isoformat(),self.clock().isoformat()))]
        return result

    def family(self,call_id):
        r=self.m.db.get(call_id);number=r['phone_number'];routing=self.m.routing.get(r)
        with self.m.db.connect() as db:
            contacts=[dict(row) for row in db.execute('SELECT contact_id,phone_number,name FROM concierge_contacts WHERE phone_number=?',(number,))]
        caller=' '.join(t['text'] for t in r['transcript'] if t['speaker']=='callee')
        names=[c['name'].split()[0] for c in contacts if c['name']]
        contradictory=any(re.search(r"\b(?:not|isn't|is not|не)\s+"+re.escape(name),caller,re.I) for name in names)
        # An explicitly different self-introduction overrides number recognition.
        claims=re.findall(r"\b(?:my name is|this is|i am|i'm)\s+([\w'-]+)",caller,re.I)
        if claims and names and any(name_key(claim) not in {name_key(n) for n in names} for claim in claims):contradictory=True
        return self.notices.for_family(contacts,number=number,ambiguous=routing['caller_identity_ambiguous'],
            contradictory=contradictory,now=self.clock())

    async def family_availability(self,call_id,args):
        from app.appointments.coaching import owner_schedule
        from app.appointments.schemas import FamilyAvailability
        if self.family(call_id).get('status')!='current':return {'status':'needs_owner'}
        request=FamilyAvailability.model_validate(args)
        if request.view!='free_busy':raise ValueError('Family lookup exposes free/busy only')
        def authorized():
            r=self.m.db.get(call_id)
            return r['status']=='in_progress' and call_id not in self.m.stopping and self.family(call_id).get('status')=='current'
        result=await owner_schedule(self,request.model_dump(mode='json'),authorized)
        result.pop('private_owner_only',None)
        result['booking_authority_expanded']=False
        return result

    async def close(self):
        if self.sync_task:
            self.sync_task.cancel();await asyncio.gather(self.sync_task,return_exceptions=True)
        if self.http:await self.http.aclose()

    def start(self):
        if not self.m.settings.appointment_sync_enabled or self.sync_task:return
        async def loop():
            while True:
                try:await self.sync.once(dry_run=False)
                except (OSError,sqlite3.Error):
                    from app.logging_config import log_event
                    log_event('appointment_index_unavailable')
                await asyncio.sleep(300)
        self.sync_task=asyncio.create_task(loop())
