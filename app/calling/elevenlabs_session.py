"""ElevenLabs' Realtime wire protocol adapted to the existing phone bridge.

No ElevenLabs hosted tools or Twilio integration are created. Our server remains
the only executor of tools, permissions, recording, transfers and booking writes.
"""
import asyncio
import base64
import hashlib
import json
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

from app.calling.live_session import BridgeFailure, MediaBridge,has_speech
from app.calling.prompts import session_config
from app.calling.audio_timing import InputAudioTimeline
from app.logging_config import log_event

SECRET_URL = 'https://api.elevenlabs.io/v1/convai/realtime/client_secrets'
SOCKET_URL = 'wss://api.elevenlabs.io/v1/convai/realtime'

MINI_CALENDAR_POLICY='''
CALENDAR TOOLS: Distinguish retrieving an EXISTING appointment from requesting a NEW booking.
EXISTING: When asked to check an appointment, call get_my_appointment using the caller's first and last name
already heard in THIS call. Ask only a missing name component. NEVER collect purpose, duration, arrangement
or desired date/time for retrieval. Do not claim to check without a real tool call. Say the verified returned
spoken_answer including its location once. Follow the server's missing_fields or clarification instruction.
After needs_name_search use search_appointment_name with the same name; ask spelling only if requested.
Pending captions are not a missing appointment. Wait; do not repeatedly ask the name or invent progress.
NEW: Collect name, purpose, duration (ordinary default fifteen minutes) and a specific location/connection.
For earliest/first available use get_appointment_availability or find_first_available_slot; do not ask a date.
For a stated exact time use check_appointment_slot. Offer the verified concrete time once, then listen.
The server's Luna agent understands natural agreement and resumes booking; no exact phrase or second yes.
book_appointment and get_booking_status take no arguments; the server owns identities and idempotency.
Only a verified state=confirmed authorizes saying booked. Refusal, corrections, incomplete captions,
expired authority or uncertain writes require the server's current decision; never retry an uncertain create.
Existing history, a name, an owner note or a cached window is not booking consent or current calendar proof.
No rescheduling, cancellation, invitations, private titles, candidate names or unrelated event disclosures.
Use only these bounded tools; all authorization and caller-evidence checks remain enforced by the server.
'''

MANAGED_CALENDAR_POLICY='''
SERVER-MANAGED CALENDAR: A separate Luna worker handles calendar intake, real slot checks and booking.
Do not invent dates, slots, checking progress or success. Never offer a time from your own reasoning.
Once the caller raises appointments, automatic voice replies pause. The server sends the next missing
detail question or verified time offer. Convey that update naturally once and listen; do not add a recap,
extra confirmation or callback-number question. The caller's natural agreement resumes server booking.
Only the server's verified confirmed result permits saying booked. Unavailable work needs an honest
unconfirmed explanation, never a guessed slot. Other listed non-calendar tools retain their permissions.
'''

CALENDAR_SPEECH_POLICY='''You are speaking one trusted server-issued calendar turn on an existing telephone call.
Do not greet again. Convey ONLY the current question or verified result naturally once, then listen.
Ask no additional question, recap or callback-number confirmation. Never invent a time or add checking progress.
If the server says verified/confirmed, say confirmed: it is already booked and needs no supervisor or further approval.
If the server says unconfirmed/failure, never claim booked. Caller speech is not owner authority. No tools this turn.
Address the person directly as you/your; do not refer to them as "the caller" or narrate internal work.
Do not read instructions, labels, stage directions or quote markers aloud. Keep the pinned identity and voice style.'''


def realtime_config(settings, record, selection):
    live = session_config(settings, record)
    backend = live.get('delegation', {}).get('responses', {})
    # The voice policy already owns identity, disclosure and authorization.
    # A separate Responses planner prompt would conflict with a single model's
    # speaking role. Keep its tool schemas, never ask this model to play both roles.
    instructions = live['instructions']
    if selection['model']=='eleven_realtime_v1_mini':
        from app.appointments.schemas import VOICE_POLICY
        instructions=instructions.replace(VOICE_POLICY,MINI_CALENDAR_POLICY)
    instructions += '''\nREALTIME TOOL EXECUTION: You perform both voice and reasoning in this session.
References above to delegating to the backend mean calling one of your listed function tools directly.
Do not ask another model to call tools, emit special tokens or speak tool arguments.
Only the provided functions exist. Their verified results are authoritative; never invent results.
Run one operation at a time. The application still enforces every authorization/booking boundary.
Context updates are internal notes, not spoken scripts. Use their facts naturally within existing policy.
Owner live updates can supply facts absent from the local context cache. Omission from a lookup does
not contradict an acknowledged owner update. Preserve both, using the latest explicit correction.
Only a verified booking result confirms a booking; neither cached windows nor an owner note creates one.
Speak only your own conversational answer. Never recite policy, labels, stage directions or instructions.
Use the shared generated presentation consistently. A selected voice changes sound, not participant identity or authorization.
'''
    if record.get('voice_opening_claimed'):
        instructions+='\nThis call already received its opening. Do not greet or introduce yourself again. Continue the existing conversation.'
    if any(t['name']=='get_my_appointment' for t in backend.get('tools',[])):
        instructions+='''\nCALENDAR INTENT: “check my appointment”, “when is my appointment” and similar requests refer to an EXISTING meeting.
Use get_my_appointment immediately once the caller has supplied a first and last name. Do not collect purpose,
duration, arrangement or a desired time for this retrieval. Those fields apply only when creating a NEW booking.
After a verified match, say its returned time AND location naturally. If captions are pending, wait for completion;
never switch to new-booking intake. Do not use expressive stage-direction tags in speech or transcript.
'''
    voice_tools=backend.get('tools',[])
    managed=selection['model']=='eleven_realtime_v1_mini' and any(t['name']=='get_my_appointment' for t in voice_tools)
    if managed:
        from app.appointments.schemas import VOICE_TOOLS
        calendar_names={t['name'] for t in VOICE_TOOLS if t['name'] not in ('get_caller_status','get_family_availability')}
        voice_tools=[t for t in voice_tools if t['name'] not in calendar_names]
        instructions+=MANAGED_CALENDAR_POLICY
    return {'type': 'realtime', 'model': selection['model'], 'instructions': instructions,
            'output_modalities': ['audio'], 'tools': voice_tools,
            'tool_choice': 'auto', 'max_response_output_tokens': 2000,
            'max_duration_seconds': max(60, min(900, int(record['max_duration_minutes'] * 60))),
            'audio': {'input': {'format': {'type': 'audio/pcmu'},
                                'transcription': {'model': 'scribe_v2_realtime'},
                                'turn_detection': None if managed else {'type': 'semantic_vad', 'eagerness': 'medium',
                                                   'create_response': True, 'interrupt_response': True}},
                      'output': {'format': {'type': 'audio/pcmu'}, 'voice': selection['voice']}}}


class Event:
    def __init__(self, data):
        self.data, self.type = data, data['type']

    def model_dump(self):
        return self.data


class AppendChannel:
    def __init__(self, owner, channel):
        self.owner, self.channel = owner, channel

    async def append(self, *, content, event_id=None, delegation_id=None):
        await self.owner.append_note(self.channel, content, event_id)


class InputAudio:
    def __init__(self, owner):
        self.owner = owner

    async def append(self, *, audio):
        if self.owner.manual_mode:
            await self.owner.manual_audio(audio);return
        await self.owner.send({'type': 'input_audio_buffer.append', 'audio': audio})


class ToolOutput:
    def __init__(self, owner):
        self.owner = owner

    async def create(self, *, item):
        name = self.owner.functions.pop(item.get('call_id'), None)
        if name == 'get_call_context':
            result = json.loads(item['output'])
            if isinstance(result, dict):
                result['owner_live_updates'] = [
                    {'mode': row['mode'], 'content': row['content']}
                    for row in self.owner.current_record().get('call_updates', [])
                    if row['status'] == 'acknowledged'][-32:]
                item = {**item, 'output': json.dumps(result, ensure_ascii=False)}
        await self.owner.send({'type': 'conversation.item.create', 'item': item})


class ResponseOutput:
    def __init__(self, owner):
        self.owner, self.item = owner, ToolOutput(owner)

    async def create(self):
        await self.owner.request_response()


class RealtimeConnection:
    """Only the small bridge-facing Live surface; not a general SDK emulation."""
    def __init__(self, socket, config, record, current_record=None, claim_opening=None,calendar_voice_instructions=None,render_speech=None):
        self.input_timeline=InputAudioTimeline()
        from app.calling.caption_verification import CaptionVerification
        self.caption_verification=CaptionVerification(self.input_timeline,max_attempts=12)
        self.input_display_times={}
        self.calendar_controlled=False
        self.calendar_managed=MANAGED_CALENDAR_POLICY in config.get('instructions','')
        self.render_speech=render_speech;self.local_events=asyncio.Queue();self.speech_tasks={}
        self.allowed_native_responses=set();self.owner_response_requested=False
        self.calendar_voice_instructions=calendar_voice_instructions or config.get('instructions','')
        self.voicing_controlled=False
        self.manual_mode=self.calendar_managed
        self.manual_active=False;self.manual_silence=0;self.manual_samples=0
        self.manual_pre=deque(maxlen=10);self.manual_pending=deque();self.manual_items={}
        self.provider_timing_required=True
        self.bridge_class = RealtimeMediaBridge
        self.socket, self.config, self.record = socket, config, record
        self.current_record = current_record or (lambda: self.record)
        self.session = SimpleNamespace(input_audio=InputAudio(self), close=self.close,
            thinking=AppendChannel(self, 'thinking'), commentary=AppendChannel(self, 'commentary'),
            instructions=AppendChannel(self, 'instructions'))
        self.response = ResponseOutput(self)
        self.started_at = time.monotonic()
        self.closing = False
        self.pending_notes = {}
        self.pending_instructions = {}
        self.pending_spoken={};self.pending_response_instruction=None
        self.spoken_items={}
        self.notes = []
        self.opening_sent=bool(record.get('transcript') or record.get('voice_opening_claimed'))
        self.claim_opening=claim_opening or (lambda:True)
        self.active_response = None
        self.response_requested = False
        self.want_response = False
        self.output_item = None
        self.output_bytes = 0
        self.cancelled = set()
        self.speech_times = {}
        self.input_text = {}
        self.output_text = {}
        self.tool_names = {t['name'] for t in config['tools']}
        self.functions = {}
        self.lock = asyncio.Lock()

    def observe_input_audio(self,start_ms,payload):
        self.input_timeline.observe(start_ms,len(payload),has_speech(payload))
        self.caption_verification.observe(payload)

    def milliseconds(self):
        return max(0, round((time.monotonic() - self.started_at) * 1000))

    async def send(self, event):
        # Never log messages, ephemeral credentials, transcripts or tool arguments.
        async with self.lock:
            await self.socket.send(json.dumps(event, ensure_ascii=False))

    async def append_instruction(self,content,event_id,*,respond=False):
        await self.append_note('instructions',content,event_id,respond=respond)

    async def append_note(self, channel, content, event_id,*,respond=False):
        if channel=='commentary':self.owner_response_requested=True
        if event_id in ('initial_greeting','startup_greeting_fallback'):
            if self.opening_sent or not self.claim_opening(): return
            self.opening_sent=True
            self.notes.append('The startup opening has been requested ONCE. Continue this call; do not greet or introduce yourself again.')
        if channel != 'commentary' and event_id != 'initial_greeting':
            # ElevenLabs accepts session.update, but silently ignores system-role
            # conversation items. Put context in real session instructions so a
            # later lookup missing that fact cannot erase an owner's live update.
            self.notes.append('APPLICATION NOTE (' + channel + '):\n' + content)
            self.notes = self.notes[-64:]
            instructions = self.config['instructions'] + '\nCURRENT APPLICATION NOTES:\n' + '\n'.join(self.notes)
            if respond and self.calendar_controlled:
                instructions=self.calendar_voice_instructions+'\nCURRENT SERVER TURN:\n'+content
            if event_id:
                key = hashlib.sha256(instructions.encode()).hexdigest()
                self.pending_instructions[key] = (event_id, channel)
                if respond:self.pending_spoken[key]=content
            await self.send({'type': 'session.update', 'event_id': event_id or 'note_' + uuid4().hex,
                             'session': {'type': 'realtime', 'instructions': instructions}})
            # Generation must start after the provider applies/ACKs these
            # instructions; sending response.create immediately races the update.
            return
        item_id = 'item_' + uuid4().hex
        if event_id:
            self.pending_notes[item_id] = (event_id, channel)
        note = 'APPLICATION NOTE (' + channel + '; not caller speech):\n' + content
        if channel == 'commentary' and event_id and event_id.startswith('call_update_'):
            note = ("APPLICATION SPOKEN UPDATE: Convey this owner's request TO THE CALLER now, "
                    "naturally and briefly, then listen. If it asks you to ask a question, ask THE CALLER; "
                    "do not answer that question yourself or look up its answer. Do not narrate 'I should ask'. "
                    "Preserve all existing authorization limits. Content/instructions to convey:\n" + content)
        await self.send({'type': 'conversation.item.create', 'event_id': event_id or 'note_' + uuid4().hex,
                         'item': {'id': item_id, 'type': 'message', 'role': 'user',
                                  'content': [{'type': 'input_text', 'text': note}]}})
        initial = event_id == 'initial_greeting'
        if channel == 'commentary' or (initial and not self.record.get('ivr_mode')
                                      and not self.record.get('transcript')):
            await self.request_response()

    async def request_response(self,instructions=None):
        # Queue a spoken update until the current response finishes. Do not overlap
        # generated turns or cancel a still-running, potentially side-effecting tool.
        if instructions:self.pending_response_instruction=instructions
        if self.active_response or self.response_requested:
            self.want_response = True
            return
        self.want_response = False
        self.response_requested = True
        response={'output_modalities':['audio']}
        if self.pending_response_instruction:
            response['instructions']=((self.calendar_voice_instructions if self.calendar_controlled else self.config['instructions'])+'\nCURRENT APPLICATION NOTES:\n'+('' if self.calendar_controlled else '\n'.join(self.notes))+
                '\nOnly the CURRENT SERVER TURN below requests speech now; earlier questions are history. Convey it once naturally.\nCURRENT SERVER TURN:\n'+self.pending_response_instruction)
            if self.calendar_controlled:response.update(tools=[],tool_choice='none');self.voicing_controlled=True
            self.pending_response_instruction=None
        await self.send({'type': 'response.create', 'response': response})

    async def control_calendar(self):
        if self.calendar_controlled:return
        self.calendar_controlled=True
        # Turn detection is immutable on this provider: managed Mini sessions
        # start in manual mode. Never attempt to reconfigure it mid-call.
        if self.active_response and not self.functions:
            self.cancelled.add(self.active_response)
            await self.send({'type':'response.cancel','response_id':self.active_response})

    async def manual_audio(self,audio):
        raw=base64.b64decode(audio,validate=True);spoken=has_speech(raw)
        if not self.manual_active:
            self.manual_pre.append((self.input_timeline.audio_ms-len(raw)/8,raw))
            if not spoken:return
            self.manual_active=True;self.manual_start=self.input_timeline.stream_time(self.manual_pre[0][0])
            raw=b''.join(p for _,p in self.manual_pre);self.manual_pre.clear()
            if self.active_response and not self.functions:
                self.cancelled.add(self.active_response)
                await self.send({'type':'response.cancel','response_id':self.active_response})
        if spoken:self.manual_end=round(self.input_timeline.stream_end)
        await self.send({'type':'input_audio_buffer.append','audio':base64.b64encode(raw).decode()})
        self.manual_samples+=len(raw);self.manual_silence=0 if spoken else self.manual_silence+len(raw)
        if self.manual_silence>=5200 or self.manual_samples>=80000:
            self.manual_pending.append((self.manual_start,self.manual_end))
            await self.send({'type':'input_audio_buffer.commit'})
            self.manual_active=False;self.manual_samples=0;self.manual_silence=0;self.manual_pre.clear()

    async def interrupt_playback(self, pending_bytes):
        if self.output_item and self.output_bytes:
            played_ms = max(0, (self.output_bytes - pending_bytes) // 8)
            await self.send({'type': 'conversation.item.truncate', 'item_id': self.output_item,
                             'content_index': 0, 'audio_end_ms': played_ms})

    async def close(self):
        self.closing = True
        tasks=list(self.speech_tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        await self.caption_verification.close()
        await self.socket.close()

    async def speak_verified(self,text,event_id,guard):
        if event_id in self.speech_tasks:return
        async def run():
            try:
                for attempt in range(2):
                    try:audio=await self.render_speech(text);break
                    except BridgeFailure as exc:
                        if attempt or exc.reason!='verified_speech_text_mismatch':raise
                if self.closing or not guard():
                    await self.local_events.put(Event({'type':'session.update_unconfirmed','client_event_id':event_id}));return
                # This is the provider's completed, text-checked generated speech,
                # not a claim that the telephone participant heard playback.
                await self.local_events.put(Event({'type':'session.instructions.appended','client_event_id':event_id}))
                start=round(self.input_timeline.stream_end)
                self.output_item=None;self.output_bytes=0
                await self.local_events.put(Event({'type':'session.output_audio.delta','delta':base64.b64encode(audio).decode()}))
                await self.local_events.put(Event({'type':'session.output_transcript.delta','event_id':event_id+'_speech',
                    'delta':text,'start_ms':start,'end_ms':start+len(audio)//8,'speech_source':'verified_render'}))
                await self.send({'type':'conversation.item.create','item':{'type':'message','role':'user','content':[{
                    'type':'input_text','text':'APPLICATION HISTORY, not caller speech. The assistant just conveyed this server-issued text; do not repeat it: '+text}]}})
            except asyncio.CancelledError:raise
            except Exception:
                await self.local_events.put(Event({'type':'error','client_event_id':event_id,'error':{'code':'verified_speech_failed'}}))
                await self.local_events.put(BridgeFailure('verified_calendar_speech_failed'))
        task=asyncio.create_task(run());self.speech_tasks[event_id]=task
        task.add_done_callback(lambda _:self.speech_tasks.pop(event_id,None))

    def transcript(self, data, speaker, text, item_id):
        if speaker=='input':
            start,end=self.speech_times.get(item_id,(None,None))
            mapped_start,mapped_end=self.input_timeline.native_span(start,end)
            if item_id in self.manual_items:
                mapped_start,mapped_end=self.manual_items[item_id]
                if not data['type'].endswith('.completed'):mapped_end=None
            display_start,display_end=self.input_display_times.get(item_id,(round(self.input_timeline.stream_end),round(self.input_timeline.stream_end)))
        else:
            mapped_start=mapped_end=round(self.input_timeline.stream_end) if self.input_timeline.segments else self.milliseconds()
            display_start=display_end=mapped_start
        event={'type':f'session.{speaker}_transcript.delta','event_id':data.get('event_id') or uuid4().hex,
            'delta':text,'start_ms':mapped_start if mapped_start is not None else display_start,
            'end_ms':max(mapped_start if mapped_start is not None else display_start,mapped_end if mapped_end is not None else display_end)}
        if speaker=='input' and mapped_end is not None:event['input_audio_end_ms']=mapped_end
        return event

    async def translate(self, data):
        """Yield bridge events only after a real provider event, including update ACKs."""
        kind = data['type']
        if kind == 'session.updated':
            instructions = data.get('session', {}).get('instructions', '')
            key = hashlib.sha256(instructions.encode()).hexdigest()
            if note := self.pending_instructions.pop(key, None):
                yield {'type': 'session.' + note[1] + '.appended', 'client_event_id': note[0]}
                if spoken:=self.pending_spoken.pop(key,None):
                    if self.calendar_controlled:
                        # A real conversation item is required: this provider can
                        # ACK instruction changes while generation still uses its
                        # initial prompt. Never treat the echo as spoken use.
                        item_id='item_'+uuid4().hex;self.spoken_items[item_id]=spoken
                        note=('APPLICATION SERVER TURN, not telephone caller speech. '
                            'The server has executed any stated calendar check/write. Convey only its requested question or verified result now:\n'+spoken)
                        await self.send({'type':'conversation.item.create','item':{'id':item_id,'type':'message','role':'user',
                            'content':[{'type':'input_text','text':note}]}})
                    else:await self.request_response(instructions=spoken)
        elif kind=='input_audio_buffer.committed':
            if self.manual_pending and data.get('item_id'):self.manual_items[data['item_id']]=self.manual_pending.popleft()
        elif kind in ('conversation.item.added', 'conversation.item.created', 'conversation.item.done'):
            item_id = data.get('item', {}).get('id')
            if note := self.pending_notes.pop(item_id, None):
                yield {'type': 'session.' + note[1] + '.appended', 'client_event_id': note[0]}
            if spoken:=self.spoken_items.pop(item_id,None):await self.request_response(instructions=spoken)
        elif kind == 'response.created':
            self.active_response = data['response']['id']
            if self.owner_response_requested:
                self.allowed_native_responses.add(self.active_response);self.owner_response_requested=False
            self.response_requested = False
            yield {'type': 'response.event', 'delegation_id': self.active_response, 'event': data}
        elif kind == 'response.output_item.added':
            if data.get('item', {}).get('type') == 'message':
                self.output_item = data['item']['id']
                self.output_bytes = 0
        elif kind == 'response.output_audio.delta':
            if (data.get('response_id') not in self.cancelled and
                    (not self.calendar_controlled or data.get('response_id') in self.allowed_native_responses)):
                self.output_bytes += len(base64.b64decode(data['delta'], validate=True))
                yield {'type': 'session.output_audio.delta', 'delta': data['delta']}
        elif kind == 'input_audio_buffer.speech_started':
            self.speech_times[data['item_id']] = (data.get('audio_start_ms'),None)
            self.input_display_times[data['item_id']]=(round(self.input_timeline.stream_end),round(self.input_timeline.stream_end))
            if self.active_response:
                self.cancelled.add(self.active_response)
            yield {'type': 'session.output_audio.clear'}
        elif kind == 'input_audio_buffer.speech_stopped':
            item_id = data['item_id']
            start=self.speech_times.get(item_id,(None,None))[0]
            end=data.get('audio_end_ms')
            self.speech_times[item_id]=(start,end)
            self.input_display_times[item_id]=(self.input_display_times.get(item_id,(round(self.input_timeline.stream_end),))[0],round(self.input_timeline.stream_end))
            covered=self.input_timeline.covered_time(end)
            if covered is not None:
                # Coverage is committed alongside a transcript, never from VAD alone.
                self.speech_times[item_id]=(start,end)
        elif kind in ('conversation.item.input_audio_transcription.delta',
                      'conversation.item.input_audio_transcription.completed'):
            item_id = data['item_id']
            previous = self.input_text.get(item_id, '')
            text = data.get('delta', '') if kind.endswith('.delta') else (
                data.get('transcript', '')[len(previous):] if data.get('transcript', '').startswith(previous) else '')
            self.input_text[item_id] = previous + text
            if kind.endswith('.completed') and not re.search(r'\w',self.input_text[item_id]):return
            from app.calling.memory import return_scope
            if (kind.endswith('.completed') and self.calendar_managed and not self.calendar_controlled
                    and (re.search(r'appointment|meeting|зустріч|запис',self.input_text[item_id],re.I)
                         or return_scope(self.input_text[item_id].casefold().replace('’',"'")))):
                await self.control_calendar()
                yield {'type':'session.output_audio.clear'}
            if text or kind.endswith('.completed'):
                yield self.transcript(data,'input',text,item_id)
            if kind.endswith('.completed') and self.manual_mode and not self.calendar_controlled:
                end=self.manual_items.get(item_id,(None,None))[1]
                latest=self.input_timeline.stream_time(self.input_timeline.utterances[-1]['end']) if self.input_timeline.utterances else None
                # A brief pause can split "my name is ... / check my appointment"
                # into two commits. Do not start a stale generic response while
                # that newer caller speech is active or waiting for its caption.
                if not self.manual_active and (end is None or latest is None or latest<=end):
                    await self.request_response()
        elif kind in ('response.output_audio_transcript.delta', 'response.output_audio_transcript.done'):
            if self.calendar_controlled and (data.get('response_id') or self.active_response) not in self.allowed_native_responses:return
            item_id = data['item_id']
            previous = self.output_text.get(item_id, '')
            text = data.get('delta', '') if kind.endswith('.delta') else (
                data.get('transcript', '')[len(previous):] if data.get('transcript', '').startswith(previous) else '')
            self.output_text[item_id] = previous + text
            if text:
                yield self.transcript(data, 'output', text, item_id)
        elif kind == 'response.output_item.done':
            item = data.get('item', {})
            if item.get('type') == 'function_call' and item.get('name') in self.tool_names:
                self.functions[item['call_id']] = item['name']
                yield {'type': 'response.event', 'delegation_id': data['response_id'], 'event': data}
        elif kind == 'response.done':
            response = data['response']
            rid = response['id']
            if self.active_response == rid:
                self.active_response = None
            self.response_requested = False
            status = response.get('status')
            yield {'type': 'response.event', 'delegation_id': rid,
                   'event': {'type': 'response.completed' if status == 'completed' else 'response.failed',
                             'response': response}}
            functions = any(i.get('type') == 'function_call' for i in response.get('output', []))
            if self.voicing_controlled:
                self.voicing_controlled=False
                if not self.want_response and not self.pending_spoken and not self.pending_response_instruction:
                    await self.send({'type':'session.update','session':{'type':'realtime','instructions':
                        self.config['instructions']+'\nCURRENT APPLICATION NOTES:\n'+'\n'.join(self.notes)}})
            if self.want_response and not functions:
                self.want_response = False
                await self.request_response()
            self.cancelled.discard(rid)
        elif kind == 'error':
            error = data.get('error', {})
            code = error.get('code', 'unknown')
            if code in ('response_cancel_not_active',):
                return
            event_id = error.get('event_id') or error.get('client_event_id')
            if event_id and event_id.startswith('call_update_'):
                yield {'type': 'error', 'client_event_id': event_id, 'error': {'code': 'provider_rejected'}}
                return
            # Provider text can contain caller data. Expose only bounded codes.
            log_event('elevenlabs_session_error', code=code if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', str(code)) else 'unknown')
            raise BridgeFailure('elevenlabs_session_error')

    async def events(self):
        async def read():
            try:
                async for raw in self.socket:
                    async for data in self.translate(json.loads(raw)):await self.local_events.put(Event(data))
                await self.local_events.put(Event({'type':'session.closed'}) if self.closing else BridgeFailure('elevenlabs_transport_closed'))
            except Exception as exc:
                if not self.closing:await self.local_events.put(exc)
        reader=asyncio.create_task(read())
        try:
            while True:
                event=await self.local_events.get()
                if isinstance(event,Exception):raise event
                yield event
                if event.type=='session.closed':return
        finally:
            reader.cancel();await asyncio.gather(reader,return_exceptions=True)

    def __aiter__(self):
        return self.events()


class RealtimeMediaBridge(MediaBridge):
    """Keep reading VAD/tool events while bursty Realtime audio plays on Twilio."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.audio_queue = asyncio.Queue()
        self.queued_bytes = 0
        self.output_busy = False
        self.pumping_bytes = 0
        self.audio_generation = 0

    async def send_live_audio(self, payload):
        if self.queued_bytes + len(payload) > 480000:  # At most one minute of PCMU.
            raise BridgeFailure('elevenlabs_playback_overflow')
        self.queued_bytes += len(payload)
        self.audio_queue.put_nowait((self.audio_generation, payload))

    async def audio_pump(self):
        while True:
            generation, payload = await self.audio_queue.get()
            self.queued_bytes -= len(payload)
            self.output_busy = True
            self.pumping_bytes = len(payload)
            try:
                await super().send_live_audio(payload, generation=generation)
            finally:
                self.output_busy = False
                self.pumping_bytes = 0

    async def clear_live_audio(self):
        self.audio_generation += 1
        # Include not-yet-submitted provider bytes when truncating heard history.
        # Conservative lower bound: a partly submitted burst is still counted
        # as unheard, rather than claiming that its unsubmitted tail was played.
        unheard = self.queued_bytes + self.pumping_bytes
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        self.queued_bytes = 0
        await self.connection.interrupt_playback(sum(self.pending_marks.values()) + unheard)
        await self.ws.send_json({'event': 'clear', 'streamSid': self.stream_sid})
        self.pending_marks.clear()
        self.playback_progress.set()  # Releases the old generation's bounded writer.

    async def finish_requested_call(self):
        await self.end_requested.wait()
        async with asyncio.timeout(30):
            while self.queued_bytes or self.output_busy:
                await asyncio.sleep(.02)
        await super().finish_requested_call()

    async def run(self, minutes):
        pump = asyncio.create_task(self.audio_pump())
        conversation = asyncio.create_task(super().run(minutes))
        try:
            done, _ = await asyncio.wait([pump, conversation], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            pump.cancel()
            conversation.cancel()
            await asyncio.gather(pump, conversation, return_exceptions=True)


@asynccontextmanager
async def started_elevenlabs_connection(settings, manager, record, selection):
    # No paid session during pre-answer ringing. The Twilio cap is a prerequisite.
    if record.get('direction') == 'inbound' and not record.get('browser_test'):
        try:
            await asyncio.wait_for(manager.gateway.prepare_inbound(
                record['twilio_call_sid'], record['max_duration_minutes']), 5)
        except Exception as exc:
            raise BridgeFailure('incoming_duration_limit_failed') from exc
    record = manager.voice_record(record['call_id'])
    config = realtime_config(settings, record, selection)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        result = await client.post(SECRET_URL,
            headers={'xi-api-key': settings.elevenlabs_api_key.get_secret_value()},
            json={'session': config, 'expires_after': {'anchor': 'created_at', 'seconds': 600}})
    if not result.is_success:
        log_event('elevenlabs_initialization_failed', call_id=record['call_id'], http_status=result.status_code)
        raise BridgeFailure('elevenlabs_initialization_rejected')
    secret = result.json()
    async with connect(SOCKET_URL, additional_headers={'Authorization': 'Bearer ' + secret['value']},
                       open_timeout=10, close_timeout=2, max_size=2**20) as socket:
        from app.calling.presentation import voice_identity
        from app.calling.identity import owner_identity_policy
        calendar_voice_instructions=(voice_identity(settings,record,inbound=True)+'\n'+owner_identity_policy(settings,record)+
            '\nPinned voice style: '+record.get('personality',{}).get('instructions','')+'\n'+CALENDAR_SPEECH_POLICY)
        from functools import partial
        from app.calling.verified_speech import render
        connection = RealtimeConnection(socket, config, record,
                                       current_record=lambda: manager.db.get(record['call_id']),
                                       claim_opening=lambda:manager.voice_backends.claim_opening(record['call_id']),
                                       calendar_voice_instructions=calendar_voice_instructions,
                                       render_speech=partial(render,settings,selection))
        async with asyncio.timeout(10):
            while True:
                event = json.loads(await socket.recv())
                if event['type'] == 'error':
                    raise BridgeFailure('elevenlabs_initialization_rejected')
                if event['type'] == 'session.created':
                    audio = event['session'].get('audio', {})
                    if (audio.get('input', {}).get('format', {}).get('type') != 'audio/pcmu'
                            or audio.get('output', {}).get('format', {}).get('type') != 'audio/pcmu'):
                        raise BridgeFailure('elevenlabs_codec_mismatch')
                    started = {'session': {'id': event['session']['id']}}
                    break
        # Opening instructions are loaded once; the existing bridge sends its
        # one-time greeting signal. Resumed keypad/owner calls retain continuity.
        # Do not store startup-only opening commands as permanent context notes.
        # The shared bridge sends the one-time, personalized greeting signal.
        log_event('elevenlabs_connected', call_id=record['call_id'], model=selection['model'])
        try:
            yield connection, record, started
        finally:
            await connection.close()
