"""Call-bound facade. Consent evidence is read from storage, never a model-provided flag."""
import asyncio,hashlib,re,sqlite3
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import httpx
from app.appointments.contracts import CalendarIdentity,ProviderFault
from app.appointments.registry import Registry,appointment,name_key
from app.appointments.service import BookingService,CallAuthority,ConsentEvidence
from app.appointments.evidence import complete_readback,caller_identified,utterances,affirmative_answer,spoken_consent
from app.appointments.disclosure import PrivateNotices
from app.appointments.schemas import SlotRequest,BookRequest,AppointmentLookup,FirstAvailableRequest
from app.appointments import confirmation
from app.appointments.details import missing_arrangement,arrangement_clarification
from app.logging_config import log_event
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


def spoken_readback(value):
    # An IANA identifier is storage data; use its unambiguous spoken equivalent.
    return readback_text(value).replace(' America/New_York,',' Eastern Time,')


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
        # Late captions can predate the queued instruction despite a newer database ID.
        if accepted and accepted.get('end_ms') is not None:
            turns=[t for t in turns if t['start_ms']>=accepted['end_ms']]
        matcher=None
        if accepted and confirmation.eligible(value,self.clock(),self.service.policy.zone,self.transcript(draft['call_id'])):
            content=accepted['content']
            short=content==confirmation.SHORT+confirmation.time_text(value)+'. Is that correct?'
            compact=content==confirmation.COMPACT+confirmation.details_text(value)+'. Is that correct?'
            if compact or (short and confirmation.established_details(self.transcript(draft['call_id']),value)):
                matcher=lambda text:complete_readback(text,value) or confirmation.matches(text,value,short=short)
        # Acknowledgment alone is insufficient: require observed assistant readback followed by caller assent.
        heard,yes=spoken_consent(turns,value,matches=matcher)
        # All current caller audio is part of the revision: any later changed detail/withdrawal fails closed.
        activity=getattr(self.m,'caller_activity',{}).get(draft['call_id'])
        caption_end=max((t['end_ms'] for t in turns if t['speaker']=='callee'),default=-1)
        audio_observed=not activity or caption_end>=activity['last_speech_ms']
        if not (accepted and accepted['status']=='acknowledged' and heard and audio_observed and yes):
            log_event('appointment_consent_evidence',call_id=draft['call_id'],
                readback_acknowledged=bool(accepted and accepted['status']=='acknowledged'),
                readback_spoken=heard,caller_affirmative=yes,captions_current=audio_observed)
        return ConsentEvidence(draft['payload_hash'],self.revision(draft['call_id']),
            bool(accepted and accepted['status']=='acknowledged' and heard and audio_observed),yes)

    def reuse_readback(self,draft):
        # A supervisor handoff/check must not repeat an already agreed identical proposal.
        # Copy only a server-bound, acknowledged, currently valid spoken proof, never a model flag.
        with self.store.connect() as db:
            candidates=[dict(r) for r in db.execute('SELECT d.* FROM appointment_drafts d JOIN appointment_readbacks r '
                'ON r.draft_id=d.draft_id AND r.draft_revision=d.revision WHERE d.call_id=? AND d.payload_hash=? '
                'ORDER BY r.created_at DESC',(draft['call_id'],draft['payload_hash']))]
        for candidate in candidates:
            try:source_authority=self.service.current(candidate)
            except ValueError:continue
            evidence=self.consent(candidate,None)
            if not (evidence.readback_observed and evidence.affirmative):continue
            with self.store.connect() as db:
                # Reusing proof cannot renew the authority under which it was obtained.
                deadline=min(source_authority.deadline,self.service.current(draft).deadline).isoformat()
                db.execute('UPDATE appointment_requests SET deadline=? WHERE request_id=?',(deadline,draft['request_id']))
                db.execute('INSERT OR IGNORE INTO appointment_readbacks SELECT ?,?,update_id,created_at,after_transcript_id '
                    'FROM appointment_readbacks WHERE draft_id=? AND draft_revision=?',
                    (draft['draft_id'],draft['revision'],candidate['draft_id'],candidate['revision']))
            return True
        return False

    def readback_plan(self,draft,*,fresh=False):
        if not fresh:
            with self.store.connect() as db:row=db.execute('SELECT update_id FROM appointment_readbacks WHERE draft_id=? AND draft_revision=?',
                (draft['draft_id'],draft['revision'])).fetchone()
            queued=self.m.db.call_update(draft['call_id'],row['update_id']) if row else None
            if queued:
                for prefix in (confirmation.SHORT,confirmation.COMPACT,'Read these quoted details exactly, without a preface, then wait:\n'):
                    if queued['content'].startswith(prefix):return prefix,queued['content'][len(prefix):]
        value=appointment(draft)
        if confirmation.eligible(value,self.clock(),self.service.policy.zone,self.transcript(draft['call_id'])):
            short=confirmation.established_details(self.transcript(draft['call_id']),value)
            prefix=confirmation.SHORT if short else confirmation.COMPACT
            text=(confirmation.time_text(value) if short else confirmation.details_text(value))+'. Is that correct?'
            return prefix,text
        return 'Read these quoted details exactly, without a preface, then wait:\n',f'{value.first_name} {value.last_name}, '+spoken_readback(value)

    async def queue_readback(self,draft,*,repeat=False):
        from uuid import UUID,uuid5
        self.service.current(draft)
        prefix,text=self.readback_plan(draft,fresh=repeat);instruction=prefix+text
        if len(text.encode())>330 or len(instruction.encode())>400:return False
        with self.m.db.connect() as db:cursor=db.execute('SELECT coalesce(max(id),0) FROM transcript_events WHERE call_id=?',(draft['call_id'],)).fetchone()[0]
        uid=uuid5(UUID(draft['draft_id']),'readback-'+str(draft['revision'])+('-after-'+str(cursor) if repeat else ''))
        with self.store.connect() as db:
            conflict=('DO UPDATE SET update_id=excluded.update_id,created_at=excluded.created_at,after_transcript_id=excluded.after_transcript_id'
                if repeat else 'DO NOTHING')
            db.execute('INSERT INTO appointment_readbacks VALUES(?,?,?,?,?) ON CONFLICT(draft_id,draft_revision) '+conflict,
                (draft['draft_id'],draft['revision'],str(uid),self.clock().isoformat(),cursor))
        await self.m.send_call_update(draft['call_id'],CallUpdateRequest(mode='instructions',content=instruction,update_id=uid))
        return True

    async def retry_consent(self,call_id,args):
        """Voice recovery only after a proven pre-write consent refusal; never replay a write."""
        args=BookRequest.model_validate(args);draft=self.service.bound(call_id,str(args.draft_id))
        self.service.current(draft)
        if self.store.prior_call_write(call_id):raise ValueError('Reconcile the existing call operation')
        check=self.store.get('appointment_checks','check_id',str(args.check_id))
        if check['draft_id']!=draft['draft_id'] or check['draft_revision']!=draft['revision']:
            raise ValueError('Check binding mismatch')
        activity=getattr(self.m,'caller_activity',{}).get(call_id)
        turns=self.transcript(call_id);answer='';last_speaker=None
        for turn in turns:
            if turn['speaker']=='callee':answer=(answer if last_speaker=='callee' else '')+turn['text']
            last_speaker=turn['speaker']
        captions=max((round(t['end_timestamp']*1000) for t in turns if t['speaker']=='callee'),default=-1)
        # This answer only permits asking again, never authorizes a calendar write.
        if not affirmative_answer(answer) or (activity and captions<activity['last_speech_ms']):
            return {'state':'needs_consent','reason':'latest_caller_answer_requires_clarification',
                'readback_queued':False,'calendar_write_attempted':False,
                'instruction':'Nothing is booked. Wait for the complete caller answer. Respect a refusal or withdrawal; '
                    'clarify corrections and check the changed details. Natural agreement is enough after a complete readback; '
                    'never require the caller to recite a booking phrase. Do not repeat the old proposal after a refusal.'}
        queued=await self.queue_readback(draft,repeat=True)
        return {'state':'needs_consent' if queued else 'needs_details',
            'reason':'complete_readback_and_fresh_assent_required' if queued else 'arrangement_too_long_for_clear_readback',
            'draft_id':draft['draft_id'],'check_id':str(args.check_id),'request_id':draft['request_id'],
            'readback_queued':queued,'calendar_write_attempted':False,
            'instruction':'Nothing is booked. The exact readback is queued; do not repeat or paraphrase it. '
                'Wait for a new caller answer after the complete readback. Only then call book_appointment with the same IDs and idempotency key. '
                'Do not retry automatically or request the supervisor solely for this recoverable consent step.' if queued
                else 'Nothing is booked. Clarify shorter exact details before checking again.'}

    async def first_available(self,call_id,args):
        args=FirstAvailableRequest.model_validate(args)
        if missing_arrangement(args.arrangement):return arrangement_clarification()
        rid=self.request(call_id,args.request_id)
        result=await self.service.find_slot(call_id,rid,args.duration_minutes,args.not_before,args.not_after)
        if result['decision']!='available':
            return {**result,'request_id':rid,'readback_queued':False,'consent_observed':False,
                'instruction':'Nothing is booked. No earliest slot was verified in the returned search window. '
                    'Explain the bounded result briefly; do not claim other dates are unavailable or repeatedly ask for a specific date/time.'}
        interval=result['interval']
        # Keep the exact-check/readback/consent/write path identical to a requested slot.
        checked=await self.check(call_id,dict(request_id=rid,first_name=args.first_name,last_name=args.last_name,
            purpose=args.purpose,arrangement=args.arrangement,start=interval.start,end=interval.end,time_zone=interval.zone))
        return {**checked,'search_start':result['search_start'],'search_end':result['search_end'],
            'selection':'first_available_in_search_window'}

    async def check(self,call_id,args):
        args=SlotRequest.model_validate(args)
        if missing_arrangement(args.arrangement):return arrangement_clarification()
        rid=self.request(call_id,args.request_id)
        draft=self.service.prepare(call_id,rid,args.appointment())
        result=await self.service.check(call_id,draft['draft_id'])
        if result['decision']=='busy':
            try:self.m.db.supervisor_request(rid)
            except KeyError:pass
            else:await self.progress(rid,'conflict',result['check_id'])
        reused=result['decision']=='available' and self.reuse_readback(draft)
        if reused:
            deadline=self.service.current(draft).deadline
            if datetime.fromisoformat(result['expires_at'])>deadline:
                result['expires_at']=deadline.isoformat()
                with self.store.connect() as db:db.execute('UPDATE appointment_checks SET expires_at=? WHERE check_id=?',
                    (result['expires_at'],result['check_id']))
        if result['decision']=='available' and not reused:
            if not await self.queue_readback(draft):return {'state':'needs_details','reason':'arrangement_too_long_for_clear_readback'}
        return {**result,'draft_id':draft['draft_id'],'request_id':rid,'schema_version':'1.0',
            'readback':self.readback_plan(draft)[1],'readback_queued':result['decision']=='available' and not reused,
            'consent_observed':reused,
            'instruction':'The identical complete readback and current caller agreement are already observed. Do not repeat the details or ask for another confirmation; call book_appointment with these IDs and a stable idempotency key. Nothing is booked yet.' if reused
                else 'The server owns the single queued readback when available. Do not add a second summary or readback. Natural contextual agreement is sufficient; never demand a prescribed booking phrase. Then call book_appointment. Nothing is booked yet.'}

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
        text=('Confirmed. '+confirmation.time_text(value)+'.' if confirmation.eligible(value,self.clock(),self.service.policy.zone,self.transcript(call_id))
            else 'Confirmed '+spoken_readback(value).removesuffix(' Is that correct?'))
        try:
            with self.store.measure('result_injection'):await self.m.send_call_update(call_id,CallUpdateRequest(mode='instructions',
                content='Verified booking. Read exactly to the caller:\n'+text,update_id=uid))
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
        brief_result=bool(update and confirmation.eligible(value,self.clock(),self.service.policy.zone)
            and update['content']=='Verified booking. Read exactly to the caller:\nConfirmed. '+confirmation.time_text(value)+'.')
        def verified_speech(text):
            if complete_readback(text,value,include_identity=False,require_confirmation=True):return True
            if brief_result:
                positive=re.fullmatch(r'\s*(?:Okay[, .]+)?Confirmed[, .]+(.+)',text,re.I)
                return bool(positive and confirmation.matches(positive[1]+' Is that correct?',value,short=True))
            return False
        for turn in utterances(turns):
            if turn['speaker']=='assistant':
                said+=turn['text']+' '
                spoken=spoken or verified_speech(said) or verified_speech(turn['text'])
            else:
                said=''
                if spoken and turn['speaker']=='callee' and normalized(turn['text']) in ('yes','correct','thankyou','thanks'):receipt=True
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
        revision=self.revision(call_id);turns=self.transcript(call_id)
        activity=getattr(self.m,'caller_activity',{}).get(call_id)
        caption_end=max((t['end_timestamp']*1000 for t in turns if t['speaker']=='callee'),default=-1)
        contradictory=not caller_identified(turns,args.first_name,args.last_name) or bool(activity and caption_end<activity['last_speech_ms'])
        result=await self.service.own_appointment(args.first_name,args.last_name,contradictory=contradictory)
        if self.revision(call_id)!=revision:return {'status':'needs_owner'}
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
