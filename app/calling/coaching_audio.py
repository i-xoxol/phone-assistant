"""Keep source identity on native audio; caption failure must not stop coaching input."""
import asyncio
import base64
import struct
import time
from collections import deque

from fastapi import WebSocketDisconnect

from app.calling.context import utcnow
from app.calling.live_session import live_client,mulaw_sample
from app.calling.transfer import remaining_seconds
from app.logging_config import log_event


def linear_mulaw(sample):
    sign=0x80 if sample<0 else 0
    magnitude=min(abs(sample),32635)+132
    exponent=min(7,max(0,magnitude.bit_length()-8))
    return (~(sign|(exponent<<4)|((magnitude>>(exponent+3))&15)))&255


def wav_pcm_to_mulaw(frames,rate):
    if rate not in (8000,16000,24000,48000):raise ValueError('Unsupported PCM sample rate')
    samples=struct.unpack('<'+'h'*(len(frames)//2),frames)
    return bytes(linear_mulaw(value) for value in samples[::rate//8000])


class NativeTurns:
    """400 ms endpointing, bounded raw PCMU pieces, one instance per verified microphone."""
    def __init__(self):
        self.pre=deque(maxlen=8);self.audio=bytearray();self.active=False;self.silence=0

    def feed(self,raw):
        spoken=bool(raw and sum(mulaw_sample(b)**2 for b in raw)/len(raw)>350**2)
        if not self.active:
            self.pre.append(raw)
            if not spoken:return []
            self.active=True;self.audio.extend(b''.join(self.pre));self.pre.clear()
        else:self.audio.extend(raw)
        self.silence=0 if spoken else self.silence+len(raw)/8000
        if self.silence>=.4:return self.flush()
        if len(self.audio)>=64000:
            piece=bytes(self.audio);self.audio.clear()
            return [(piece,False)]  # A long utterance continues; do not ask for an answer yet.
        return []

    def flush(self):
        value=bytes(self.audio);self.audio.clear();self.active=False;self.silence=0;self.pre.clear()
        return [(value,True)] if value else []


async def capture_with_coaching(ws,row,side,stream,offset,*,on_audio=None,on_mark=None,receive_timeout=None):
    """Direct raw audio and optional portal captions run independently after common signature binding."""
    from app.calling.transcription import AudioTurns,open_transcription,configure
    m=ws.app.state.manager;tid=row['transfer_id'];speaker='owner' if side=='owner' else 'callee'
    queue=asyncio.Queue(maxsize=150);clock=time.monotonic()

    async def captions():
        try:
            async with live_client(m.settings) as client:
                async with await open_transcription(client) as connection:
                    await configure(connection,m.settings)
                    m.observer.stream_state(tid,side,'streaming')
                    items={}
                    async def receive():
                        async for event in connection:
                            data=event.model_dump();kind=data['type']
                            if kind=='error':raise RuntimeError('Caption provider unavailable')
                            if kind not in ('conversation.item.input_audio_transcription.delta',
                                    'conversation.item.input_audio_transcription.completed'):continue
                            item=data['item_id'];stamp=offset+int((time.monotonic()-clock)*1000)
                            entry=items.setdefault(item,{'text':'','start':stamp})
                            entry['text']=(entry['text']+data.get('delta','') if kind.endswith('.delta') else data.get('transcript',''))
                            turn='human-'+stream+'-'+item
                            if entry['text']:m.db.human_caption(row['call_id'],turn,speaker,entry['start'],stamp,entry['text'])
                            if kind.endswith('.completed'):m.observer.transcript_changed(tid,speaker,entry['text'],turn)
                    async def send():
                        turns=AudioTurns()
                        while True:
                            raw=await queue.get()
                            if raw is None:await turns.commit(connection);return
                            await turns.feed(connection,raw)
                    reader=asyncio.create_task(receive());sender=asyncio.create_task(send())
                    try:
                        done,_=await asyncio.wait((reader,sender),return_when=asyncio.FIRST_COMPLETED)
                        for task in done:task.result()
                        if sender in done:await asyncio.sleep(.8)
                        elif m.observer.current(m.transfer.get(tid)):
                            m.observer.stream_state(tid,side,'degraded')
                    finally:
                        reader.cancel();sender.cancel();await asyncio.gather(reader,sender,return_exceptions=True)
        except asyncio.CancelledError:raise
        except Exception as exc:
            m.observer.stream_state(tid,side,'degraded')
            log_event('owner_caption_unavailable',call_id=row['call_id'],side=side,error_type=type(exc).__name__)

    caption_task=asyncio.create_task(captions());native=NativeTurns();native_enabled=True
    def coach(audio,complete):
        nonlocal native_enabled
        if not native_enabled:return
        try:m.observer.realtime.audio_turn(tid,side,audio,complete=complete)
        except Exception as exc:
            native_enabled=False
            m.observer.task(m.observer.realtime.close(tid))
            log_event('owner_native_coaching_unavailable',call_id=row['call_id'],error_type=type(exc).__name__)
    try:
        async with asyncio.timeout(remaining_seconds(m.db.get(row['call_id']))):
            while True:
                message=await asyncio.wait_for(ws.receive_json(),receive_timeout)
                if message.get('streamSid')!=stream:raise ValueError('Microphone stream changed')
                if message.get('event')=='stop':break
                if message.get('event')=='mark' and on_mark:
                    on_mark(str(message.get('mark',{}).get('name','')))
                if message.get('event')!='media':continue
                media=message['media']
                if media.get('track')!='inbound':raise ValueError('Wrong microphone track')
                raw=base64.b64decode(media['payload'],validate=True)
                if len(raw)>8000:raise ValueError('Microphone frame too large')
                if on_audio:on_audio(raw)
                for audio,complete in native.feed(raw):
                    coach(audio,complete)
                if not caption_task.done():
                    try:queue.put_nowait(raw)
                    except asyncio.QueueFull:
                        caption_task.cancel();m.observer.stream_state(tid,side,'degraded')
    except WebSocketDisconnect:pass
    finally:
        for audio,complete in native.flush():coach(audio,complete)
        if not caption_task.done():
            try:queue.put_nowait(None)
            except asyncio.QueueFull:caption_task.cancel()
            try:await asyncio.wait_for(caption_task,2)
            except (TimeoutError,asyncio.CancelledError):caption_task.cancel()
        await asyncio.gather(caption_task,return_exceptions=True)
