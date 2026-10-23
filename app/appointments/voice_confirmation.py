"""Short spoken proposals with proof from audio playback, not conversational ASR.

The complete private draft remains authoritative. Only its concrete date/time
needs to be offered aloud after intake. Natural assent is evaluated separately
from the assistant's wording; calendar policy and dispatch guards are unchanged.
"""
import asyncio
import re
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo

from app.appointments import confirmation
from app.appointments.evidence import affirmative_answer,ambiguous_agreement,speech_words
from app.appointments.fixed_speech import fixed_speech
from app.appointments.registry import appointment
from app.appointments.service import ConsentEvidence
from app.storage.models import CallUpdateRequest
from app.logging_config import log_event


QUESTION='Does that work for you?'


class VoiceConfirmation:
    def __init__(self,gateway):
        self.g=gateway;self.synthesizer=fixed_speech;self.audio_cache={};self.preparations={}
        with self.g.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS appointment_voice_prompts(
                update_id TEXT PRIMARY KEY,call_id TEXT NOT NULL,draft_id TEXT NOT NULL,
                draft_revision INTEGER NOT NULL,payload_hash TEXT NOT NULL,kind TEXT NOT NULL,
                text TEXT NOT NULL,state TEXT NOT NULL,session_id TEXT NOT NULL,
                start_ms INTEGER,payload_end_ms INTEGER,end_ms INTEGER,error_code TEXT)''')

    def row(self,update_id):
        with self.g.store.connect() as db:r=db.execute('SELECT * FROM appointment_voice_prompts WHERE update_id=?',(update_id,)).fetchone()
        return dict(r) if r else None

    def proposal(self,draft):
        with self.g.store.connect() as db:r=db.execute("SELECT p.* FROM appointment_readbacks r JOIN appointment_voice_prompts p ON p.update_id=r.update_id WHERE r.draft_id=? AND r.draft_revision=? AND p.kind='proposal'",
            (draft['draft_id'],draft['revision'])).fetchone()
        return dict(r) if r else None

    def update(self,uid,**fields):
        assert set(fields)<={'state','start_ms','payload_end_ms','end_ms','error_code'}
        with self.g.store.connect() as db:db.execute('UPDATE appointment_voice_prompts SET '+','.join(k+'=?' for k in fields)+' WHERE update_id=?',(*fields.values(),uid))

    def bridge(self,call_id):
        b=self.g.m.bridges.get(call_id)
        return b if b and getattr(b,'server_booking',False) else None

    async def issue(self,draft,uid,*,kind='proposal'):
        b=self.bridge(draft['call_id']);value=appointment(draft)
        if not b:return False
        start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
        slot=start.strftime('%A')+', '+confirmation.time_text(value)+'.'
        text=slot if kind=='proposal' else self.result_text(value)
        with self.g.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO appointment_voice_prompts VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)',
                (str(uid),draft['call_id'],draft['draft_id'],draft['revision'],draft['payload_hash'],kind,text,'queued',b.session_id))
        b.owned_booking=str(uid)
        if kind=='proposal':
            deadline=self.g.service.current(draft).deadline
            if b.booking_expiry:b.booking_expiry.cancel()
            def expire():
                if b.owned_booking!=str(uid) or b.closing or b.handoff:return
                self.update(str(uid),state='withdrawn',error_code='request_expired')
                b.owned_booking=None
                operation=self.g.store.prior_call_write(b.call_id)
                sent=bool(operation and operation['write_attempted'])
                message=('The booking request expired while an operation may have reached the calendar. Do not claim success or absence and do not create again. Use the existing operation status or offer owner review.' if sent else
                    'The booking request expired before any calendar write. Nothing was booked by this request. Explain that once; do not keep saying you are waiting for confirmation. A new request needs a new availability check.')
                task=asyncio.create_task(b.connection.session.instructions.append(delegation_id=None,
                    content=message))
                self.g.m.tool_tasks.add(task)
                def done(t):
                    self.g.m.tool_tasks.discard(t)
                    if not t.cancelled():t.exception()
                task.add_done_callback(done)
            b.booking_expiry=asyncio.get_running_loop().call_later(max(.01,(deadline-self.g.clock()).total_seconds()),expire)
        await self.g.m.send_call_update(draft['call_id'],CallUpdateRequest(mode='instructions',update_id=uid,
            content='Server-owned appointment audio: '+text+(' '+QUESTION if kind=='proposal' else '')))
        return True

    def valid(self,row,b):
        if (self.g.m.bridges.get(b.call_id) is not b or b.closing or b.handoff or b.end_requested.is_set()
                or b.session_id!=row['session_id'] or b.owned_booking!=row['update_id']
                or self.g.m.db.get(b.call_id)['status']!='in_progress'):return False
        draft=self.g.service.bound(b.call_id,row['draft_id'])
        if draft['revision']!=row['draft_revision'] or draft['payload_hash']!=row['payload_hash']:return False
        if row['kind']=='proposal':self.g.service.current(draft)
        else:
            operation=self.g.store.prior_call_write(b.call_id)
            if not operation or operation['state']!='confirmed' or operation['payload_hash']!=row['payload_hash']:return False
        return True

    def start(self,b,row):
        if row['state']!='queued':return
        # The update pump must not wait for synthesis/playback while the tool
        # awaits its reply. All incoming audio/captions continue independently.
        task=asyncio.create_task(self.play(b,row));b.booking_audio_tasks.add(task)
        self.g.m.tool_tasks.add(task)
        def done(t):b.booking_audio_tasks.discard(t);self.g.m.tool_tasks.discard(t)
        task.add_done_callback(done)

    async def packet(self,text,voice,validate):
        for attempt in range(2):
            try:return await self.synthesizer(self.g.m.settings,text,voice,validate)
            except (ValueError,RuntimeError,TimeoutError):
                if attempt:raise

    def result_text(self,value):
        start=value.interval.start.astimezone(ZoneInfo(value.interval.zone))
        return 'Great, see you '+start.strftime('%A')+' at '+start.strftime('%I:%M %p').lstrip('0')+'. Confirmed.'

    def prepare(self,text,voice,validate):
        key=(text,voice)
        if key not in self.preparations:
            async def run():
                if key in self.audio_cache:return self.audio_cache[key]
                audio=await self.packet(text,voice,validate)
                if len(self.audio_cache)>=32:self.audio_cache.clear()
                self.audio_cache[key]=audio;return audio
            task=asyncio.create_task(run());self.preparations[key]=task
            def done(t):
                self.preparations.pop(key,None)
                if not t.cancelled():t.exception()  # A speculative result may never be needed.
            task.add_done_callback(done)
        return self.preparations[key]

    async def play(self,b,row):
        uid=row['update_id'];cid=b.call_id
        try:
            if not self.valid(row,b):raise ValueError('Superseded proposal')
            self.update(uid,state='preparing')
            voice=self.g.m.voice_record(cid)['voice_name']
            value=appointment(self.g.service.bound(cid,row['draft_id']))
            if row['kind']=='proposal':
                result_text=self.result_text(value)
                # Prepare, but never play, the success packet while checking the
                # offer. After assent only calendar I/O and playback remain.
                self.prepare(result_text,voice,lambda t:speech_words(t)==speech_words(result_text))
                def validate(t):
                    t=re.sub(r'^\s*'+value.interval.start.astimezone(ZoneInfo(value.interval.zone)).strftime('%A')+r'\b[,\s]*','',t,flags=re.I)
                    return confirmation.matches(t+' Is that correct?',value,short=True)
                audio,question=await asyncio.gather(asyncio.shield(self.prepare(row['text'],voice,validate)),
                    asyncio.shield(self.prepare(QUESTION,voice,lambda t:speech_words(t)==speech_words(QUESTION))))
            else:
                audio=await asyncio.shield(self.prepare(row['text'],voice,lambda t:speech_words(t)==speech_words(row['text'])));question=None
            if not self.valid(row,b):raise ValueError('Superseded proposal')
            started=b.media_time_ms();self.update(uid,state='playing',start_ms=started)
            await b.play_booking_audio(audio,uid+'-payload')
            if not self.valid(row,b):raise ValueError('Interrupted playback')
            end=b.media_time_ms();self.update(uid,state='payload_played',payload_end_ms=end)
            self.caption(b,uid+'-payload',row['text'],started,end)
            if question:
                await b.play_booking_audio(question,uid+'-question')
                if not self.valid(row,b):raise ValueError('Interrupted playback')
                self.caption(b,uid+'-question',QUESTION,end,b.media_time_ms())
            self.update(uid,state='played',end_ms=b.media_time_ms())
            self.g.m.db.finish_update(cid,uid,b.session_id,'acknowledged',start_ms=started,end_ms=b.media_time_ms())
            self.g.voice_workflow.notify(b)
            if row['kind']=='result':
                await b.connection.session.thinking.append(delegation_id=None,content='The server has played the verified booking confirmation. The appointment is confirmed. Do not repeat its details or ask for approval. Listen and close politely when the caller is done.')
                b.owned_booking=None
        except asyncio.CancelledError:
            self.update(uid,state='failed',error_code='playback_interrupted');raise
        except Exception as exc:
            self.update(uid,state='failed',error_code='audio_unconfirmed')
            self.g.m.db.finish_update(cid,uid,b.session_id,'unconfirmed',error_code='audio_unconfirmed')
            log_event('appointment_audio_unconfirmed',call_id=cid,error_type=type(exc).__name__)
            if b.owned_booking==uid:
                b.owned_booking=None
                await b.connection.session.instructions.append(delegation_id=None,content='The appointment audio could not be delivered. Do not claim a new booking succeeded. Explain the audio problem once and offer owner review; do not keep saying you are waiting for confirmation.')

    def caption(self,b,event_id,text,start,end):
        # This is an explicit server audio/playback record, not an ASR claim.
        self.g.m.db.transcript(b.call_id,dict(type='server.booking_audio.played',speaker='assistant',
            event_id='server-booking:'+event_id,delta=text,start_ms=start,end_ms=end))

    def evidence(self,draft):
        row=self.proposal(draft)
        if not row:return None
        b=self.bridge(draft['call_id']);revision=self.g.revision(draft['call_id'])
        observed=bool(b and row['state']=='played' and row['payload_end_ms'] is not None
            and row['session_id']==b.session_id and row['payload_hash']==draft['payload_hash'])
        answer='';last_end=None
        if observed:
            with self.g.m.db.connect() as db:
                turns=[dict(r) for r in db.execute("SELECT * FROM transcript_events WHERE call_id=? AND speaker='callee' AND start_ms>=? AND end_ms>=? ORDER BY start_ms,id",
                    (draft['call_id'],row['start_ms'],row['payload_end_ms']))]
            for turn in turns:
                # A complete refusal/correction invalidates this offered slot;
                # a later unrelated yes cannot revive it.
                if last_end is not None and turn['start_ms']-last_end>1500:
                    if not affirmative_answer(answer):observed=False
                    answer=''
                answer+=turn['text'];last_end=turn['end_ms']
            activity=getattr(self.g.m,'caller_activity',{}).get(draft['call_id'])
            if activity and (last_end is None or last_end<activity['last_speech_ms']):
                return ConsentEvidence(draft['payload_hash'],revision,observed,False)
        return ConsentEvidence(draft['payload_hash'],revision,observed,observed and affirmative_answer(answer))

    async def release_answer(self,b):
        bound=self.g.voice_workflow.bound(b.call_id)
        if not bound:return
        draft,_=bound;row=self.proposal(draft)
        if not row or row['state']!='played' or b.owned_booking!=row['update_id']:return
        with self.g.m.db.connect() as db:turns=[dict(r) for r in db.execute("SELECT * FROM transcript_events WHERE call_id=? AND speaker='callee' AND start_ms>=? ORDER BY start_ms,id",
            (b.call_id,row['start_ms']))]
        if not turns:return
        activity=getattr(self.g.m,'caller_activity',{}).get(b.call_id)
        if activity and turns[-1]['end_ms']<activity['last_speech_ms']:return
        answer=''.join(t['text'] for t in turns if t['end_ms']>=row['payload_end_ms'])
        if not answer or affirmative_answer(answer):return
        b.owned_booking=None
        if not ambiguous_agreement(answer):self.update(row['update_id'],state='withdrawn')
        await b.connection.session.instructions.append(delegation_id=None,content='The server played the exact appointment time once. Respond to the caller’s latest answer. Respect a refusal or changed details; collect only the correction and check it. For uncertainty ask only “Does that work for you?” Do not repeat the proposal or demand a booking phrase.')

    async def close(self):
        tasks=list(self.preparations.values())
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
