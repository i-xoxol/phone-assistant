"""Two existing phone legs, paced local relay, and owner-only native coaching audio."""
import asyncio
import base64
import hmac
import re
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from datetime import datetime
from uuid import uuid4

from fastapi import WebSocketDisconnect
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse

from app.calling.coaching_audio import capture_with_coaching,linear_mulaw
from app.calling.context import utcnow
from app.calling.live_session import mulaw_sample
from app.calling.transfer import remaining_seconds
from app.logging_config import log_event

FRAME=160  # 20 ms, PCMU/8 kHz, mono. No WAV headers on either WebSocket.
SILENCE=b'\xff'*FRAME
DECODE=tuple(mulaw_sample(i) for i in range(256))
ENCODE=bytes(linear_mulaw(i) for i in range(-32768,32768))


def mix_private(caller,coach):
    """Caller is reduced only during a hint. Saturate linear PCM before re-encoding G.711."""
    if not coach:return caller
    if len(caller)!=len(coach):raise ValueError('Unequal audio frame lengths')
    return bytes(ENCODE[max(-32768,min(32767,int(DECODE[a]*.45)+DECODE[b]))+32768]
        for a,b in zip(caller,coach))


class AudioBuffer:
    def __init__(self):self.data=bytearray();self.dropped=0
    def put(self,raw):
        self.data.extend(raw)
        excess=max(0,len(self.data)-FRAME*10)  # Never accumulate more than 200 ms of human speech.
        if excess:del self.data[:excess];self.dropped+=excess
    def take(self):
        raw=bytes(self.data[:FRAME]);del self.data[:FRAME]
        return raw+SILENCE[:FRAME-len(raw)]


class Leg:
    def __init__(self,ws,stream,handler=None):
        self.ws=ws;self.stream=stream;self.input=AudioBuffer();self.write_lock=asyncio.Lock()
        self.handler=handler


class Room:
    def __init__(self,bridge,tid):
        self.b=bridge;self.tid=tid;self.legs={};self.ready=asyncio.Event();self.closed=asyncio.Event()
        self.call_id=bridge.m.transfer.get(tid)['call_id']
        self.pump=None;self.hint=None;self.late_frames=0

    def attach(self,side,leg):
        if side in self.legs or self.closed.is_set():raise ValueError('Duplicate bridge leg')
        self.legs[side]=leg
        if len(self.legs)==2:
            self.b.update(self.tid,state='active');self.ready.set()
            self.pump=self.b.o.task(self.relay())
            self.b.event(self.tid,'connected')

    def hint_frame(self):
        pool=self.b.o.realtime;ch=pool.channels.get(self.tid);marks=[]
        if not ch or not ch.connected.is_set():self.hint=None;return None,marks
        while True:
            if self.hint:
                generation,snapshot,raw=self.hint
                if not pool.valid(self.tid,ch,generation,snapshot):self.hint=None;continue
                frame=raw[:FRAME];rest=raw[FRAME:]
                self.hint=(generation,snapshot,rest) if rest else None
                return (generation,snapshot,frame+SILENCE[:FRAME-len(frame)]),marks
            try:generation,snapshot,packet=ch.queue.get_nowait()
            except asyncio.QueueEmpty:return None,marks
            if not pool.valid(self.tid,ch,generation,snapshot):continue
            if isinstance(packet,dict):marks.append(packet['mark'])
            else:self.hint=(generation,snapshot,packet)

    async def send(self,side,raw,hint=None,marks=()):
        leg=self.legs[side]
        async with leg.write_lock:
            if self.closed.is_set():return
            # The caller output is ALWAYS the owner's raw microphone, never a mixed frame.
            if side=='owner':
                pool=self.b.o.realtime;ch=pool.channels.get(self.tid)
                for wid in marks:
                    if ch and wid in ch.marks and not ch.marks[wid].done():
                        await asyncio.wait_for(leg.ws.send_json({'event':'mark','streamSid':leg.stream,
                            'mark':{'name':wid}}),.3)
                if hint and ch and pool.valid(self.tid,ch,hint[0],hint[1]):raw=mix_private(raw,hint[2])
            await asyncio.wait_for(leg.ws.send_json({'event':'media','streamSid':leg.stream,
                'media':{'payload':base64.b64encode(raw).decode()}}),.3)

    async def relay(self):
        deadline=time.monotonic()+.04  # A small initial jitter cushion, never a finished-clip buffer.
        try:
            while not self.closed.is_set() and self.call_id not in self.b.m.stopping:
                await asyncio.sleep(max(0,deadline-time.monotonic()))
                if time.monotonic()-deadline>.1:
                    self.late_frames+=1;deadline=time.monotonic()  # Do not send stale audio in a catch-up burst.
                deadline+=.02
                caller=self.legs['caller'].input.take();owner=self.legs['owner'].input.take()
                try:hint,marks=self.hint_frame()
                except Exception as exc:
                    # Model/context failures cannot break the two human audio paths.
                    hint,marks=None,[];self.hint=None
                    self.b.o.task(self.b.o.realtime.close(self.tid))
                    log_event('owner_bridge_hint_unavailable',error_type=type(exc).__name__)
                await asyncio.gather(self.send('caller',owner),self.send('owner',caller,hint,marks))
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.b.event(self.tid,'relay_failed',error_type=type(exc).__name__)
            await self.b.fallback(self.tid,'relay_failed')


class OwnerAudioBridge:
    def __init__(self,observer):
        self.o=observer;self.m=observer.m;self.rooms={}
        with self.m.db.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS owner_audio_bridges (
                transfer_id TEXT PRIMARY KEY REFERENCES owner_transfers(transfer_id),
                nonce TEXT NOT NULL,state TEXT NOT NULL,caller_stream_sid TEXT,owner_stream_sid TEXT)''')
            # Existing sockets have closed on restart. Their inline TwiML falls back; never reconnect blindly.
            db.execute("UPDATE owner_audio_bridges SET state='fallback' WHERE state IN ('waiting','active')")

    def record(self,tid):
        with self.m.db.connect() as db:row=db.execute('SELECT * FROM owner_audio_bridges WHERE transfer_id=?',(tid,)).fetchone()
        return dict(row) if row else None

    def update(self,tid,**values):
        if not values.keys()<={'state','caller_stream_sid','owner_stream_sid'}:raise ValueError('Unknown bridge field')
        with self.m.db.connect() as db:db.execute('UPDATE owner_audio_bridges SET '+','.join(k+'=?' for k in values)+
            ' WHERE transfer_id=?',(*values.values(),tid))

    def in_fallback(self,tid):
        record=self.record(tid)
        return bool(record and record['state'] in ('fallback','completed'))

    def prepare(self,tid):
        with self.m.db.connect() as db:db.execute('INSERT OR IGNORE INTO owner_audio_bridges VALUES (?,?,?,NULL,NULL)',
            (tid,uuid4().hex,'waiting'))
        record=self.record(tid)
        if record['state'] in ('waiting','active') and tid not in self.rooms:self.rooms[tid]=Room(self,tid)
        return record

    def event(self,tid,state,**fields):
        self.m.db.emit(self.m.transfer.get(tid)['call_id'],'call.owner_coaching',{'state':'bridge_'+state,**fields})
        log_event('owner_audio_bridge',call_id=self.m.transfer.get(tid)['call_id'],state=state,**fields)

    def twiml(self,row,side):
        tid=row['transfer_id'];record=self.prepare(tid)
        if record['state'] not in ('waiting','active'):return self.o.conference_twiml(row,side,fallback=True)
        response=VoiceResponse()
        if side=='caller':response.say('I will try to reach the owner. Please hold. The conversation will be transcribed.')
        else:self.m.screening.append_prompt(response,row,'connected-coaching')
        stream=response.connect().stream(url=self.m.settings.public_url('/twilio/owner-bridge/'+tid+'/'+side,websocket=True))
        stream.parameter(name='nonce',value=record['nonce'])
        # This is already in Twilio's hands before the socket opens. No live server callback is needed
        # to enter the two-person fallback conference when the bridge/server connection fails.
        root=ET.fromstring(str(response))
        root.extend(list(ET.fromstring(self.o.conference_twiml(row,side,fallback=True))))
        return ET.tostring(root,encoding='unicode')

    async def wait_ready(self,tid):
        room=self.rooms.get(tid)
        if not room or self.in_fallback(tid):raise ValueError('Bridge unavailable')
        await asyncio.wait_for(room.ready.wait(),15)
        if room.closed.is_set() or self.in_fallback(tid):raise ValueError('Bridge ended')

    async def clear(self,tid):
        room=self.rooms.get(tid)
        if not room:return
        room.hint=None;leg=room.legs.get('owner')
        if leg:
            with suppress(Exception):
                async with asyncio.timeout(.5):
                    async with leg.write_lock:await leg.ws.send_json({'event':'clear','streamSid':leg.stream})

    async def close_room(self,tid):
        room=self.rooms.pop(tid,None)
        if not room:return
        room.closed.set();room.ready.set();room.hint=None
        for side,leg in room.legs.items():
            key=(room.call_id,side)
            if self.o.streams.get(key) is leg.handler:self.o.streams.pop(key,None)
        if room.pump and room.pump is not asyncio.current_task():
            room.pump.cancel();await asyncio.gather(room.pump,return_exceptions=True)
        async def close(leg):
            with suppress(Exception):await asyncio.wait_for(leg.ws.close(),1)
        await asyncio.gather(*(close(leg) for leg in room.legs.values()))
        self.event(tid,'closed',dropped_bytes=sum(leg.input.dropped for leg in room.legs.values()),late_frames=room.late_frames)

    async def fallback(self,tid,reason):
        record=self.record(tid)
        if not record or record['state'] in ('fallback','completed'):return
        self.update(tid,state='fallback')
        row=self.m.transfer.get(tid)
        if row['accepted'] and row['mode']=='whisper':self.m.transfer.update(tid,mode='transcript')
        self.event(tid,'fallback',reason=reason)
        # Close the two media sockets together. Their embedded TwiML joins the SAME ordinary room.
        await self.close_room(tid)
        await self.o.realtime.close(tid)

    async def end(self,tid):
        if self.record(tid):self.update(tid,state='completed')
        await self.close_room(tid)

    async def media(self,ws,tid,side):
        s=self.m.settings;bound=False;room=None
        signature=ws.headers.get('x-twilio-signature','')
        urls=[s.public_url(ws.url.path,websocket=wss)+suffix for wss in (True,False) for suffix in ('','/')]
        token=s.twilio_auth_token.get_secret_value()
        if side not in ('caller','owner') or not token or not any(
                RequestValidator(token).validate(url,{},signature) for url in urls):await ws.close(code=1008);return
        try:
            row=self.m.transfer.get(tid);record=self.record(tid);room=self.rooms.get(tid)
            if (row['strategy']!='bridge' or not self.o.current(row) or not record or not room
                    or record['state'] not in ('waiting','active') or side in room.legs
                    or (side=='owner' and not row['accepted'])):raise ValueError('Inactive bridge')
            await ws.accept()
            async with asyncio.timeout(8):
                while True:
                    message=await ws.receive_json()
                    if message.get('event')=='connected':continue
                    start=message.get('start',{});params=start.get('customParameters',{})
                    latest=self.m.transfer.get(tid)
                    expected=self.m.db.get(row['call_id'])['twilio_call_sid'] if side=='caller' else latest['child_sid']
                    stream=start.get('streamSid','')
                    if (message.get('event')!='start' or start.get('accountSid')!=s.twilio_account_sid
                            or not expected or start.get('callSid')!=expected
                            or not hmac.compare_digest(str(params.get('nonce','')),record['nonce'])
                            or start.get('mediaFormat')!={'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}
                            or start.get('tracks') not in (None,['inbound'])
                            or not re.fullmatch(r'MZ[0-9a-zA-Z_]{1,80}',stream)
                            or not self.o.current(latest) or self.in_fallback(tid)):
                        raise ValueError('Bridge microphone binding mismatch')
                    break
            key=(row['call_id'],side)
            if side in room.legs or key in self.o.streams:raise ValueError('Duplicate microphone')
            with self.m.db.connect() as db:
                field=side+'_stream_sid'
                claimed=db.execute('UPDATE owner_audio_bridges SET '+field+'=? WHERE transfer_id=? AND '+field+
                    " IS NULL AND state IN ('waiting','active')",(stream,tid)).rowcount
            if not claimed:raise ValueError('Bridge microphone already bound')
            leg=Leg(ws,stream,asyncio.current_task());room.attach(side,leg);bound=True
            self.o.streams[key]=asyncio.current_task()
            parent=self.m.db.get(row['call_id'])
            offset=max(0,int((utcnow()-datetime.fromisoformat(parent['started_at'] or parent['created_at'])).total_seconds()*1000))
            await capture_with_coaching(ws,row,side,stream,offset,on_audio=leg.input.put,
                on_mark=lambda wid:self.o.realtime.acknowledge(tid,wid) if side=='owner' else None,receive_timeout=10)
        except asyncio.CancelledError:raise
        except (WebSocketDisconnect,ValueError,RuntimeError,TimeoutError) as exc:
            if bound:self.event(tid,'stream_ended',side=side,error_type=type(exc).__name__)
        finally:
            if bound:
                key=(row['call_id'],side)
                if self.o.streams.get(key) is asyncio.current_task():self.o.streams.pop(key,None)
                if self.o.current(self.m.transfer.get(tid)):
                    # The ASGI socket task can be cancelled on disconnect. Keep peer recovery
                    # in an independently tracked task so that cancellation cannot strand it.
                    self.o.task(self.fallback(tid,'media_connection_ended'))
            with suppress(Exception,asyncio.CancelledError):await ws.close()

    async def stop(self):
        await asyncio.gather(*(self.fallback(tid,'service_stopping') for tid in list(self.rooms)),return_exceptions=True)
