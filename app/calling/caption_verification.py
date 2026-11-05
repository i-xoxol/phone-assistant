"""On-demand independent ASR proof for ambiguous timestamp-free native captions."""
import asyncio,base64,re
from collections import deque
from app.logging_config import log_event


def normalized(text):return re.sub(r'[^\w]','',text.casefold())


async def transcribe(settings,raw):
    # Reuse the already-configured, input-only owner transcription transport.
    from app.calling.live_session import live_client
    from app.calling.transcription import open_transcription,configure,pcm24
    async with asyncio.timeout(7):
        async with live_client(settings) as client:
            async with await open_transcription(client) as connection:
                await configure(connection,settings)
                await connection.send({'type':'input_audio_buffer.append','audio':base64.b64encode(pcm24(raw)).decode()})
                await connection.send({'type':'input_audio_buffer.commit'})
                async for event in connection:
                    data=event.model_dump()
                    if data['type']=='error':raise RuntimeError('Caption verification rejected')
                    if data['type']=='conversation.item.input_audio_transcription.completed':return data.get('transcript','')
    return ''


class CaptionVerification:
    def __init__(self,timeline):
        self.timeline=timeline;self.frames=deque();self.task=None;self.attempts=0;self.checked=None;self.proof=None
        self.transcribe=transcribe

    def observe(self,raw):
        self.frames.append((self.timeline.audio_ms-len(raw)/8,raw))
        while self.frames and self.frames[0][0]<self.timeline.audio_ms-15000:self.frames.popleft()

    def notify(self,bridge):
        if self.task and not self.task.done():return
        gateway=bridge.manager.appointments
        if not gateway:return
        caller=''.join(t['text'] for t in bridge.manager.db.get(bridge.call_id)['transcript'] if t['speaker']=='callee')[-2000:]
        if not gateway.voice_workflow.bound(bridge.call_id) and not re.search(r'appointment|meeting|зустріч|запис',caller,re.I):return
        task=asyncio.create_task(self.verify(bridge));self.task=task

    async def verify(self,bridge):
        m=bridge.manager;cid=bridge.call_id
        def current():
            record=m.db.get(cid)
            return (m.bridges.get(cid) is bridge and cid not in m.stopping and m.settings.appointments_enabled
                and record['status']=='in_progress' and record['direction']=='inbound' and record['inbound_mode']=='concierge'
                and not (bridge.closing or bridge.handoff or bridge.end_requested.is_set()))
        try:
            # Native ASR may arrive just before the final quiet endpoint.
            await asyncio.sleep(.55)
            if not current() or not m.settings.openai_api_key.get_secret_value():return
            activity=m.caller_activity.get(cid,{})
            if activity.get('caption_covered_ms',-1)>=activity.get('last_speech_ms',0):return
            spans=[s for s in self.timeline.utterances if s['closed'] and s['start']>=self.frames[0][0]] if self.frames else []
            if not spans:return
            start=spans[0]['start'];end=spans[-1]['end'];covered=self.timeline.covered_time(end)
            if covered is None or covered+bridge.transcript_offset_ms<activity.get('last_speech_ms',0):return
            key=(start,end)
            if self.checked==key:
                if self.proof is None:return
                text=self.proof
            else:
                if self.attempts>=4:return
                self.checked=key;self.proof=None;self.attempts+=1
                raw=b''.join(frame for at,frame in self.frames if start-200<=at<end+200)
                if len(raw)<800 or len(raw)>120000:return
                text=await self.transcribe(m.settings,raw);self.proof=text
            if not current():return
            # This proof covers exactly the captured clip. New speech stays pending.
            activity=m.caller_activity.get(cid,{})
            if activity.get('last_speech_ms',0)>covered+bridge.transcript_offset_ms:return
            caller=''.join(t['text'] for t in m.db.get(cid)['transcript'] if t['speaker']=='callee')
            if len(normalized(text))<3 or not normalized(caller).endswith(normalized(text)):return
            activity['caption_covered_ms']=max(activity.get('caption_covered_ms',-1),covered+bridge.transcript_offset_ms)
            if m.appointments:
                m.appointments.voice_workflow.notify(bridge)
                m.appointments.lookup_intent.notify(bridge)
            log_event('native_caption_verified',call_id=cid)
        except asyncio.CancelledError:raise
        except Exception as exc:
            log_event('native_caption_verification_unavailable',error_type=type(exc).__name__)

    async def close(self):
        if self.task and not self.task.done():self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        self.frames.clear()
