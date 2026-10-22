"""Private conference coaching: source-labelled raw microphones -> Realtime -> owner-only PCMU."""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from contextlib import suppress
from uuid import uuid4

from fastapi import WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException
from twilio.twiml.voice_response import VoiceResponse

from app.calling.context import utcnow
from app.calling.realtime_coaching import RealtimeCoachSession
from app.calling.transfer import remaining_seconds
from app.logging_config import log_event


def facts_key(facts):
    return hashlib.sha256(json.dumps(facts,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class Channel:
    def __init__(self):
        self.changed=asyncio.Event()
        self.connected=asyncio.Event()
        self.closed=asyncio.Event()
        self.queue=asyncio.Queue(maxsize=240)
        self.session=None
        self.socket=None
        self.stream_sid=None
        self.job=None
        self.worker=None
        self.starter=None
        self.generation=0
        self.last_automatic=0
        self.clear_lock=asyncio.Lock()
        self.marks={}
        self.audio_history=[]
        self.pending_audio=False
        self.held_owner_audio=None
        self.transfer_id=None
        self.call_id=None
        self.delivery_permission=None
        self.delivery_until=0


class OwnerRealtime:
    def __init__(self,observer):
        self.o=observer;self.m=observer.m
        self.channels={}
        self.factory=RealtimeCoachSession
        with self.m.db.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS owner_realtime_coaches (
                transfer_id TEXT PRIMARY KEY REFERENCES owner_transfers(transfer_id),
                coach_id TEXT NOT NULL UNIQUE,nonce TEXT NOT NULL,created_at TEXT NOT NULL,
                participant_sid TEXT,media_call_sid TEXT,status TEXT NOT NULL,error_code TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS owner_realtime_hints (
                hint_id TEXT PRIMARY KEY,transfer_id TEXT NOT NULL REFERENCES owner_transfers(transfer_id),
                created_at TEXT NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL,facts_hash TEXT NOT NULL,
                source_turn TEXT,first_audio_ms INTEGER)''')
            db.execute("UPDATE owner_realtime_coaches SET status='interrupted',error_code='service_restarted' "
                "WHERE status NOT IN ('completed','failed','unconfirmed','interrupted')")

    def available(self):
        s=self.m.settings
        return s.owner_coaching_strategy=='realtime' and s.owner_speech_enabled and (
            s.owner_realtime_transport=='bridge' or bool(s.owner_coaching_application_sid))

    def uses_bridge(self):
        return self.available() and self.m.settings.owner_realtime_transport=='bridge'

    def active(self,tid):
        row=self.m.transfer.get(tid)
        return (self.available() and row['accepted'] and row['mode']=='whisper' and self.o.current(row)
            and not (row['strategy']=='bridge' and self.o.audio_bridge.in_fallback(tid)))

    def record(self,tid):
        with self.m.db.connect() as db:
            row=db.execute('SELECT * FROM owner_realtime_coaches WHERE transfer_id=?',(tid,)).fetchone()
        return dict(row) if row else None

    def update(self,tid,**values):
        allowed={'participant_sid','media_call_sid','status','error_code'}
        if not values.keys()<=allowed:raise ValueError('Unknown coach field')
        with self.m.db.connect() as db:
            db.execute('UPDATE owner_realtime_coaches SET '+','.join(k+'=?' for k in values)+
                ' WHERE transfer_id=?',(*values.values(),tid))

    def state(self,tid,state,**values):
        self.m.db.emit(self.m.transfer.get(tid)['call_id'],'call.owner_coaching',
            {'state':'realtime_'+state,**values})
        log_event('owner_realtime_state',call_id=self.m.transfer.get(tid)['call_id'],state=state,**values)

    def ensure(self,tid):
        if not self.active(tid):return None
        if tid in self.channels:return self.channels[tid]
        # One application participant per transfer. Never retry an uncertain create.
        if self.record(tid):return None
        with self.m.db.connect() as db:
            db.execute('INSERT INTO owner_realtime_coaches VALUES (?,?,?,?,NULL,NULL,?,NULL)',
                (tid,str(uuid4()),uuid4().hex,utcnow().isoformat(),'preparing'))
        ch=self.channels[tid]=Channel()
        ch.transfer_id=tid
        ch.call_id=self.m.transfer.get(tid)['call_id']
        ch.session=self.factory(self.m.settings,lambda state,**v:self.state(tid,state,**v))
        if self.m.settings.owner_calendar_coaching_enabled and self.m.appointments:
            async def calendar_lookup(args):
                from app.appointments.coaching import owner_schedule
                generation=ch.generation
                def authorized():
                    return (self.active(tid) and self.channels.get(tid) is ch and tid not in self.o.paused
                        and ch.generation==generation and ch.call_id not in self.m.stopping)
                result=await owner_schedule(self.m.appointments,args,authorized)
                if result.get('status')=='current':
                    ch.delivery_until=min(time.monotonic()+30,time.monotonic()+max(0,
                        (datetime.fromisoformat(result['expires_at'])-utcnow()).total_seconds()))
                return result
            ch.session.calendar_lookup=calendar_lookup
        ch.starter=self.o.task(self.start(tid,ch))
        ch.worker=self.o.task(self.work(tid,ch))
        return ch

    async def start(self,tid,ch):
        create_attempted=False
        peer_wait_started=False
        try:
            await asyncio.wait_for(ch.session.ready.wait(),12)
            if ch.session.closed.is_set():raise RuntimeError('Coach provider unavailable')
            if self.m.transfer.get(tid)['strategy']=='bridge':
                peer_wait_started=True
                await self.o.audio_bridge.wait_ready(tid)
                if not self.active(tid):return
                ch.connected.set();self.update(tid,status='streaming');self.state(tid,'streaming')
                return
            # Accepting the keypad prompt precedes the owner's <Dial><Conference>.
            # A room SID proves only that the caller joined, not that the owner is coachable.
            row=await self.wait_for_owner(tid)
            if row is None:
                self.update(tid,status='failed',error_code='owner_left_before_coach')
                await self.close(tid,from_starter=True);return
            record=self.record(tid)
            self.update(tid,status='dialing')
            create_attempted=True
            participant=await asyncio.wait_for(self.m.gateway.add_coach(
                row,record,max(1,remaining_seconds(self.m.db.get(row['call_id'])))),8)
            self.check_participant(row,record,participant)
            self.bind_participant(tid,participant.call_sid)
            if not self.active(tid):
                await self.m.gateway.hangup(participant.call_sid);return
            self.state(tid,'connecting')
            await asyncio.wait_for(ch.connected.wait(),15)
        except asyncio.CancelledError:raise
        except Exception as exc:
            # A timed-out create may have succeeded. Keep its durable record; no redial.
            rejected=isinstance(exc,TwilioRestException) and exc.status in (400,401,403,404,422,429)
            status='unconfirmed' if create_attempted and not rejected else 'failed'
            code=getattr(exc,'code',None)
            safe_code=code if type(code) is int and 0<code<1000000 else None
            self.update(tid,status=status,error_code='twilio_'+str(safe_code) if safe_code else 'coach_setup_'+status)
            self.state(tid,'unavailable',error_type=type(exc).__name__,twilio_code=safe_code)
            if peer_wait_started and self.m.transfer.get(tid)['strategy']=='bridge' and self.o.current(self.m.transfer.get(tid)):
                # Losing coaching readiness cannot leave a partial human bridge waiting forever.
                await self.o.audio_bridge.fallback(tid,'peer_not_ready')
            await self.close(tid,from_starter=True)

    async def wait_for_owner(self,tid):
        """Only repeat read-only readiness checks; the participant create still happens once."""
        async with asyncio.timeout(15):
            while self.active(tid):
                row=self.m.transfer.get(tid)
                if row['conference_sid']:
                    try:
                        owner=await asyncio.wait_for(self.m.gateway.coach_participant(
                            row['conference_sid'],row['child_sid']),3)
                    except TwilioRestException as exc:
                        if exc.status!=404:raise
                    else:
                        if owner.conference_sid!=row['conference_sid'] or owner.call_sid!=row['child_sid']:
                            raise ValueError('Owner conference binding mismatch')
                        if owner.status in ('complete','failed'):raise ValueError('Owner left conference')
                        if owner.status=='connected' and owner.hold is False:
                            return row if self.active(tid) else None
                await asyncio.sleep(.2)
        return None

    def check_participant(self,row,record,participant):
        if (participant.conference_sid!=row['conference_sid'] or participant.label!='rt-'+record['coach_id']
                or participant.coaching is not True or participant.call_sid_to_coach!=row['child_sid']
                or not re.fullmatch(r'CA[0-9a-fA-F]{32}',participant.call_sid or '')):
            raise ValueError('Private coach binding mismatch')

    def bind_participant(self,tid,sid):
        record=self.record(tid)
        if record['participant_sid'] and record['participant_sid']!=sid:raise ValueError('Coach SID mismatch')
        self.update(tid,participant_sid=sid)

    async def connect_twiml(self,form):
        record=None
        with self.m.db.connect() as db:
            row=db.execute('SELECT * FROM owner_realtime_coaches WHERE coach_id=?',(str(form.get('coach_id','')),)).fetchone()
            if row:record=dict(row)
        if not record or not hmac.compare_digest(record['nonce'],str(form.get('nonce',''))):
            raise ValueError('Unknown coach')
        tid=record['transfer_id'];row=self.m.transfer.get(tid)
        if not self.active(tid) or record['status'] not in ('dialing','streaming'):raise ValueError('Inactive coach')
        if form.get('ApplicationSid') and form['ApplicationSid']!=self.m.settings.owner_coaching_application_sid:
            raise ValueError('Coach application mismatch')
        # Fetch by unique label also handles a signed webhook arriving before create returns.
        participant=await asyncio.wait_for(self.m.gateway.coach_participant(row['conference_sid'],'rt-'+record['coach_id']),5)
        self.check_participant(row,record,participant)
        sid=str(form.get('CallSid',''))
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}',sid):raise ValueError('Invalid coach media SID')
        if sid!=participant.call_sid:
            actual=await asyncio.wait_for(self.m.gateway.fetch(sid),5)
            if actual.parent_call_sid!=participant.call_sid:raise ValueError('Unknown application child')
        if record['media_call_sid'] and record['media_call_sid']!=sid:raise ValueError('Coach media rebound')
        self.bind_participant(tid,participant.call_sid)
        self.update(tid,media_call_sid=sid)
        response=VoiceResponse()
        stream=response.connect().stream(url=self.m.settings.public_url('/twilio/owner-coach/media/'+tid,websocket=True))
        stream.parameter(name='coach_id',value=record['coach_id'])
        stream.parameter(name='nonce',value=record['nonce'])
        response.hangup()
        return str(response)

    def known_leg(self,tid,sid):
        record=self.record(tid)
        return bool(record and sid and sid in (record['participant_sid'],record['media_call_sid']))

    def changed(self,tid,explicit=False):
        ch=self.ensure(tid)
        if not ch:return
        ch.changed.set()
        if explicit and ch.job and not ch.job.done():
            ch.generation+=1
            ch.job.cancel()

    def audio_turn(self,tid,side,raw,complete=True):
        if side not in ('caller','owner') or not raw or not self.active(tid):return
        ch=self.ensure(tid)
        if not ch:return
        if tid in self.o.paused:
            if side=='owner':ch.held_owner_audio={'speaker':side,'audio':raw}
            return
        ch.audio_history.append({'speaker':side,'audio':raw})
        while len(ch.audio_history)>8 or sum(len(t['audio']) for t in ch.audio_history)>240000:
            ch.audio_history.pop(0)
        if not complete:return
        ch.pending_audio=True
        if tid not in self.o.paused:
            ch.changed.set()
            if side=='owner' and ch.job and not ch.job.done():
                ch.generation+=1;ch.job.cancel()

    def labels_changed(self,tid,resumed=False):
        # Captions supplement speaker identity and pause controls; they do not trigger native answers.
        ch=self.channels.get(tid)
        if resumed and ch and ch.held_owner_audio:
            ch.audio_history=[ch.held_owner_audio];ch.held_owner_audio=None
            ch.pending_audio=True;ch.changed.set()

    def context_changed(self):
        for tid,ch in list(self.channels.items()):
            ch.delivery_permission=None
            if not self.active(tid) or tid in self.o.paused or not ch.audio_history:continue
            ch.pending_audio=True;ch.changed.set();ch.generation+=1
            if ch.job and not ch.job.done():ch.job.cancel()

    async def bind_conference_event(self,tid,form):
        record=self.record(tid)
        if not record or form.get('ParticipantLabel')!='rt-'+record['coach_id']:
            raise ValueError('Unknown conference leg')
        row=self.m.transfer.get(tid)
        participant=await asyncio.wait_for(self.m.gateway.coach_participant(row['conference_sid'],form['CallSid']),5)
        self.check_participant(row,record,participant)
        if participant.call_sid!=form['CallSid']:raise ValueError('Conference coach SID mismatch')
        self.bind_participant(tid,participant.call_sid)

    def context(self,tid,request):
        row=self.m.transfer.get(tid);record=self.m.db.get(row['call_id'])
        facts=sorted(self.o.facts(row),key=lambda f:f['fact_id'])[:80]
        recent=record['transcript'][-16:]
        with self.m.db.connect() as db:
            previous=[r['text'] for r in db.execute('SELECT text FROM owner_realtime_hints '
                "WHERE transfer_id=? AND state IN ('streamed','played') ORDER BY created_at DESC LIMIT 3",(tid,))]
        return {'OWNER':self.m.settings.caller_name,'owner_pronouns':self.m.settings.owner_pronouns,
            'now':utcnow().isoformat(),'call_objective':record['objective'][:1200],
            'call_constraints':record['constraints'],'CURRENT_APPROVED_FACTS':facts,
            'recent_captions':[{'speaker':t['speaker'],'text':t['text'][:600]} for t in recent],
            'previous_private_hints':list(reversed(previous)),
            'explicit_owner_request':request['text'] if request else None,
            'trigger':'owner_request' if request else 'consider_proactive_hint'}

    async def work(self,tid,ch):
        try:
            while self.active(tid) and not ch.closed.is_set():
                await ch.changed.wait();ch.changed.clear()
                await asyncio.sleep(.12)  # Coalesce adjacent caption completions, not multi-second batching.
                if tid in self.o.paused:continue
                if not self.active(tid):return
                await asyncio.wait_for(ch.connected.wait(),25)
                if not self.active(tid) or tid in self.o.paused:continue
                if not ch.pending_audio:continue
                ch.pending_audio=False
                self.o.requests.pop(tid,None)
                ch.generation+=1
                ch.job=asyncio.create_task(self.hint(tid,ch,None,ch.generation,turns=list(ch.audio_history)))
                try:await ch.job
                except asyncio.CancelledError:
                    if ch.closed.is_set():return
                except Exception as exc:
                    self.state(tid,'hint_unavailable',error_type=type(exc).__name__)
                    if ch.session.closed.is_set():return
                finally:ch.job=None
                ch.last_automatic=time.monotonic()
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.state(tid,'unavailable',error_type=type(exc).__name__)
        finally:
            if ch.session and ch.session.closed.is_set():await self.close(tid)

    async def hint(self,tid,ch,request,generation,turns=None):
        context=self.context(tid,request);snapshot=facts_key(context['CURRENT_APPROVED_FACTS'])
        if not context['CURRENT_APPROVED_FACTS'] and not request and not turns:return
        with self.m.db.connect() as db:
            count=db.execute("SELECT count(*) FROM owner_realtime_hints WHERE transfer_id=? AND state!='skipped'",(tid,)).fetchone()[0]
        if count>=self.m.settings.owner_realtime_coaching_max_hints:return
        wid=str(uuid4());started=time.monotonic()
        with self.m.db.connect() as db:
            db.execute('INSERT INTO owner_realtime_hints VALUES (?,?,?,?,?,?,?,NULL)',
                (wid,tid,utcnow().isoformat(),'','generating',snapshot,request['turn_id'] if request else None))
        first=None
        ch.delivery_permission=(generation,snapshot)
        expiries=[datetime.fromisoformat(f['expires_at']) for f in context['CURRENT_APPROVED_FACTS'] if f.get('expires_at')]
        ch.delivery_until=time.monotonic()+max(0,min(30,(min(expiries)-utcnow()).total_seconds()) if expiries else 8)
        async def audio(raw):
            nonlocal first
            if not self.valid(tid,ch,generation,snapshot):raise ValueError('Superseded coaching context')
            if first is None:first=round((time.monotonic()-started)*1000)
            for pos in range(0,len(raw),320):
                await ch.queue.put((generation,snapshot,raw[pos:pos+320]))
        try:
            result=await ch.session.ask(context,audio,turns=turns)
            if not self.valid(tid,ch,generation,snapshot):raise ValueError('Superseded hint')
            state='skipped' if not result['audio_bytes'] else 'streamed'
            with self.m.db.connect() as db:
                db.execute('UPDATE owner_realtime_hints SET text=?,state=?,first_audio_ms=? WHERE hint_id=?',
                    (result['text'],state,first,wid))
            if result['audio_bytes']:
                played=ch.marks[wid]=asyncio.get_running_loop().create_future()
                await ch.queue.put((generation,snapshot,{'mark':wid}))
                try:await asyncio.wait_for(played,result['audio_bytes']/8000+5)
                finally:ch.marks.pop(wid,None)
            self.state(tid,state,first_audio_ms=first)
        except BaseException:
            with self.m.db.connect() as db:db.execute("UPDATE owner_realtime_hints SET state='cancelled' WHERE hint_id=?",(wid,))
            await self.clear(ch)
            raise

    def valid(self,tid,ch,generation,snapshot):
        return (self.active(tid) and tid not in self.o.paused and generation==ch.generation
            and snapshot==facts_key(sorted(self.o.facts(self.m.transfer.get(tid)),key=lambda f:f['fact_id'])[:80]))

    def valid_frame(self,tid,ch,generation,snapshot):
        """Hot path is memory-only; authority is validated before generation and invalidated on changes."""
        return (self.channels.get(tid) is ch and not ch.closed.is_set() and tid not in self.o.paused
            and ch.call_id not in self.m.stopping and ch.generation==generation
            and ch.delivery_permission==(generation,snapshot) and time.monotonic()<ch.delivery_until)

    async def clear(self,ch):
        async with ch.clear_lock:
            ch.delivery_permission=None
            while not ch.queue.empty():
                with suppress(asyncio.QueueEmpty):ch.queue.get_nowait()
            for future in ch.marks.values():
                if not future.done():future.cancel()
            if ch.socket and ch.stream_sid:
                with suppress(Exception):await ch.socket.send_json({'event':'clear','streamSid':ch.stream_sid})
            if ch.transfer_id and self.m.transfer.get(ch.transfer_id)['strategy']=='bridge':
                await self.o.audio_bridge.clear(ch.transfer_id)

    def acknowledge(self,tid,wid):
        ch=self.channels.get(tid)
        future=ch.marks.get(wid) if ch else None
        if not future or future.done():return
        with self.m.db.connect() as db:db.execute("UPDATE owner_realtime_hints SET state='played' "
            "WHERE hint_id=? AND transfer_id=? AND state='streamed'",(wid,tid))
        future.set_result(None)

    async def pause(self,tid):
        ch=self.channels.get(tid)
        if ch:
            ch.generation+=1
            ch.audio_history.clear();ch.held_owner_audio=None;ch.pending_audio=False;ch.changed.clear()
            if ch.job:ch.job.cancel()
            await ch.session.cancel()
            await self.clear(ch)

    async def media(self,ws,tid):
        s=self.m.settings
        signature=ws.headers.get('x-twilio-signature','')
        urls=[s.public_url(ws.url.path,websocket=wss)+suffix for wss in (True,False) for suffix in ('','/')]
        if not s.twilio_auth_token.get_secret_value() or not any(RequestValidator(s.twilio_auth_token.get_secret_value()).validate(
                url,{},signature) for url in urls):await ws.close(code=1008);return
        ch=self.channels.get(tid);record=self.record(tid)
        if not ch or not record or not self.active(tid) or ch.socket:await ws.close(code=1008);return
        await ws.accept()
        try:
            async with asyncio.timeout(8):
                while True:
                    data=await ws.receive_json()
                    if data.get('event')=='connected':continue
                    start=data.get('start',{});params=start.get('customParameters',{})
                    if (data.get('event')!='start' or start.get('accountSid')!=s.twilio_account_sid
                            or start.get('callSid')!=record['media_call_sid'] or not record['media_call_sid']
                            or params.get('coach_id')!=record['coach_id']
                            or not hmac.compare_digest(str(params.get('nonce','')),record['nonce'])
                            or start.get('mediaFormat')!={'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}
                            or not re.fullmatch(r'MZ[0-9a-zA-Z_]{1,80}',start.get('streamSid',''))):
                        raise ValueError('Coach stream mismatch')
                    break
            # Recheck owner-only routing at attachment, not just when participant creation was requested.
            row=self.m.transfer.get(tid)
            participant=await asyncio.wait_for(self.m.gateway.coach_participant(row['conference_sid'],record['participant_sid']),5)
            self.check_participant(row,record,participant)
            if ch.socket or not self.active(tid):raise ValueError('Duplicate or ended coach stream')
            ch.socket=ws;ch.stream_sid=start['streamSid'];ch.connected.set()
            self.update(tid,status='streaming');self.state(tid,'streaming')
            async def receive():
                while True:
                    data=await ws.receive_json()
                    if data.get('streamSid')!=ch.stream_sid:raise ValueError('Coach stream changed')
                    if data.get('event')=='stop':return
                    if data.get('event')=='mark':
                        wid=str(data.get('mark',{}).get('name',''))
                        with self.m.db.connect() as db:db.execute("UPDATE owner_realtime_hints SET state='played' "
                            "WHERE hint_id=? AND transfer_id=? AND state='streamed'",(wid,tid))
                        future=ch.marks.get(wid)
                        if future and not future.done():future.set_result(None)
                    # Inbound room audio is deliberately discarded. Both labelled mic captions already exist.
            async def send():
                while self.active(tid):
                    generation,snapshot,packet=await ch.queue.get()
                    if not self.valid_frame(tid,ch,generation,snapshot):await self.clear(ch);continue
                    if isinstance(packet,dict):
                        await ws.send_json({'event':'mark','streamSid':ch.stream_sid,'mark':{'name':packet['mark']}})
                    else:
                        await ws.send_json({'event':'media','streamSid':ch.stream_sid,
                            'media':{'payload':base64.b64encode(packet).decode()}})
                        await asyncio.sleep(len(packet)/8000)
            sender=asyncio.create_task(send());receiver=asyncio.create_task(receive())
            try:
                done,_=await asyncio.wait((sender,receiver),return_when=asyncio.FIRST_COMPLETED,
                    timeout=remaining_seconds(self.m.db.get(row['call_id'])))
                for task in done:task.result()
            finally:
                sender.cancel();receiver.cancel()
                await asyncio.gather(sender,receiver,return_exceptions=True)
        except asyncio.CancelledError:raise
        except (WebSocketDisconnect,ValueError,RuntimeError,TimeoutError):pass
        finally:
            if ch.socket is ws:
                ch.socket=None;ch.connected.clear()
                await self.close(tid)
            with suppress(Exception):await ws.close()

    async def close(self,tid,from_starter=False):
        record=self.record(tid)
        if not record:return
        with self.m.db.connect() as db:
            claimed=db.execute("UPDATE owner_realtime_coaches SET status='closing' WHERE transfer_id=? "
                "AND status NOT IN ('closing','completed')",(tid,)).rowcount
        if not claimed:return
        ch=self.channels.pop(tid,None)
        if ch:
            ch.closed.set();ch.changed.set();ch.generation+=1
            for task in (ch.job,ch.worker,None if from_starter else ch.starter):
                if task and task is not asyncio.current_task():task.cancel()
            await self.clear(ch)
            await ch.session.close()
            if ch.socket:
                with suppress(Exception):await ch.socket.close()
        row=self.m.transfer.get(tid)
        try:
            sid=record['participant_sid']
            if row['strategy']=='bridge':
                self.update(tid,status=record['status'] if record['status'] in ('failed','unconfirmed') else 'completed')
                return
            if not sid and record['status'] in ('dialing','unconfirmed') and row['conference_sid']:
                participant=await asyncio.wait_for(self.m.gateway.coach_participant(row['conference_sid'],'rt-'+record['coach_id']),4)
                self.check_participant(row,record,participant);sid=participant.call_sid
                self.bind_participant(tid,sid)
            if sid and sid not in (row['child_sid'],self.m.db.get(row['call_id'])['twilio_call_sid']):
                await asyncio.wait_for(self.m.gateway.hangup(sid),5)
            self.update(tid,status=record['status'] if record['status'] in ('failed','unconfirmed') else 'completed')
        except Exception:
            self.update(tid,status='unconfirmed',error_code='coach_cleanup_unconfirmed')
            self.state(tid,'cleanup_unconfirmed')

    async def stop(self):
        await asyncio.gather(*(self.close(tid) for tid in list(self.channels)),return_exceptions=True)

    def history(self,cid):
        with self.m.db.connect() as db:
            return [dict(r) for r in db.execute('SELECT h.created_at,h.text,h.state FROM owner_realtime_hints h '
                "JOIN owner_transfers t USING(transfer_id) WHERE t.call_id=? AND h.text!='' ORDER BY h.created_at",(cid,))]
