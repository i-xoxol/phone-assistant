"""Signed Twilio microphone streams to dedicated OpenAI transcription, never audio output."""
import asyncio
import base64
import struct
import time
import re
from collections import deque
from contextlib import suppress

from fastapi import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from app.calling.context import utcnow
from app.calling.live_session import live_client,mulaw_sample
from app.calling.transfer import remaining_seconds
from app.logging_config import log_event


def transcription_config(settings):
    return {'type':'transcription','audio':{'input':{'format':{'type':'audio/pcm','rate':24000},
        'transcription':{'model':settings.openai_transcription_model,'delay':'low'},'turn_detection':None}}}


def pcm24(raw):
    # Telephony has only 8 kHz bandwidth. Repeat samples to match the API's 24 kHz PCM transport.
    return b''.join(struct.pack('<hhh',*(mulaw_sample(b),)*3) for b in raw)


class AudioTurns:
    """Small application VAD; gpt-live-transcribe does not support server VAD."""
    def __init__(self):
        self.pre=deque(maxlen=10)
        self.active=False
        self.silence=0
        self.samples=0

    async def feed(self,connection,raw):
        spoken=bool(raw and sum(mulaw_sample(b)**2 for b in raw)/len(raw)>350**2)
        duration=len(raw)/8000
        if not self.active:
            self.pre.append(raw)
            if not spoken: return spoken
            self.active=True
            raw=b''.join(self.pre);self.pre.clear()
        await connection.send({'type':'input_audio_buffer.append','audio':base64.b64encode(pcm24(raw)).decode()})
        self.samples+=len(raw)
        self.silence=0 if spoken else self.silence+duration
        if self.silence>=.65 or self.samples>=80000:
            await self.commit(connection)
        return spoken

    async def commit(self,connection):
        if self.active and self.samples>=800:
            await connection.send({'type':'input_audio_buffer.commit'})
        self.active=False;self.samples=0;self.silence=0;self.pre.clear()


async def open_transcription(client):
    return client.realtime.connect(extra_query={'intent':'transcription'},max_retries=0)


async def configure(connection,settings):
    await connection.send({'type':'session.update','session':transcription_config(settings)})
    async with asyncio.timeout(10):
        async for event in connection:
            data=event.model_dump()
            if data['type'] in ('session.updated','transcription_session.updated'): return
            if data['type']=='error': raise RuntimeError('Transcription configuration rejected')
    raise RuntimeError('Transcription closed before ready')


async def handle_owner_media(ws,tid,side):
    m=ws.app.state.manager;s=m.settings
    signature=ws.headers.get('x-twilio-signature','')
    urls=[s.public_url(ws.url.path,websocket=wss)+suffix for wss in (True,False) for suffix in ('','/')]
    if not s.twilio_auth_token.get_secret_value() or side not in ('caller','owner') or not any(RequestValidator(s.twilio_auth_token.get_secret_value()).validate(
            url,{},signature) for url in urls):
        await ws.close(code=1008);return
    try:
        row=m.transfer.get(tid)
        if (row['strategy']!='conference' or not m.observer.current(row)
                or (side=='owner' and not row['accepted'])): raise ValueError('Inactive transfer')
    except (ValueError,KeyError):
        await ws.close(code=1008);return
    key=(row['call_id'],side)
    if key in m.observer.streams:
        await ws.close(code=1008);return
    await ws.accept()
    bound=False
    try:
        async with asyncio.timeout(10):
            while True:
                message=await ws.receive_json()
                if message.get('event')=='connected': continue
                start=message.get('start',{})
                current=m.transfer.get(tid)
                expected=m.db.get(row['call_id'])['twilio_call_sid'] if side=='caller' else current['child_sid']
                if (message.get('event')!='start' or start.get('accountSid')!=s.twilio_account_sid
                        or not expected or start.get('callSid')!=expected or key in m.observer.streams
                        or start.get('mediaFormat')!={'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}
                        or start.get('tracks') not in (None,['inbound'])):
                    raise ValueError('Microphone binding mismatch')
                stream=start.get('streamSid','')
                if not re.fullmatch(r'MZ[0-9a-zA-Z_]{1,80}',stream): raise ValueError('Invalid microphone stream')
                m.observer.streams[key]=asyncio.current_task();bound=True
                break
        from datetime import datetime
        record=m.db.get(row['call_id'])
        offset=max(0,int((utcnow()-datetime.fromisoformat(record['started_at'] or record['created_at'])).total_seconds()*1000))
        speaker='owner' if side=='owner' else 'callee'
        async with live_client(s) as client:
            async with await open_transcription(client) as connection:
                await configure(connection,s)
                m.observer.stream_state(tid,side,'streaming')
                captions={}
                async def receive():
                    async for event in connection:
                        data=event.model_dump();kind=data['type']
                        if kind=='error': raise RuntimeError('Transcription provider error')
                        if kind not in ('conversation.item.input_audio_transcription.delta',
                                'conversation.item.input_audio_transcription.completed'): continue
                        item=data['item_id']
                        caption=captions.setdefault(item,{'text':'','start':offset+int((time.monotonic()-clock)*1000)})
                        caption['text']=(caption['text']+data.get('delta','') if kind.endswith('.delta') else data.get('transcript',''))
                        if caption['text']:
                            m.db.human_caption(row['call_id'],'human-'+stream+'-'+item,speaker,
                                caption['start'],offset+int((time.monotonic()-clock)*1000),caption['text'])
                        if kind.endswith('.completed'):
                            m.observer.transcript_changed(tid)
                async def feed():
                    turns=AudioTurns()
                    while True:
                        try: message=await ws.receive_json()
                        except WebSocketDisconnect:
                            await turns.commit(connection);return
                        if message.get('streamSid')!=stream: raise ValueError('Stream SID mismatch')
                        if message.get('event')=='stop':
                            await turns.commit(connection);return
                        if message.get('event')!='media': continue
                        media=message['media']
                        if media.get('track')!='inbound': raise ValueError('Unexpected media track')
                        raw=base64.b64decode(media['payload'],validate=True)
                        if len(raw)>8000: raise ValueError('Audio frame too large')
                        if await turns.feed(connection,raw): m.observer.last_speech[tid]=time.monotonic()
                clock=time.monotonic()
                receiver=asyncio.create_task(receive());feeder=asyncio.create_task(feed())
                try:
                    done,_=await asyncio.wait((receiver,feeder),timeout=remaining_seconds(record),return_when=asyncio.FIRST_COMPLETED)
                    for task in done: task.result()
                    if receiver in done and m.observer.current(m.transfer.get(tid)):
                        raise RuntimeError('Transcription closed before telephone stream')
                    if feeder in done and not receiver.done():
                        # Let the final committed utterance arrive after the telephone closes.
                        await asyncio.sleep(3)
                    if m.observer.current(m.transfer.get(tid)):
                        m.observer.stream_state(tid,side,'degraded')
                finally:
                    receiver.cancel();feeder.cancel()
                    await asyncio.gather(receiver,feeder,return_exceptions=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if bound and not isinstance(exc,WebSocketDisconnect):
            m.observer.stream_state(tid,side,'degraded')
            log_event('owner_transcription_failed',call_id=row['call_id'],side=side,error_type=type(exc).__name__)
    finally:
        if bound: m.observer.streams.pop(key,None)
        with suppress(Exception): await ws.close()
