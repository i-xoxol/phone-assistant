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
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.calling.live_session import BridgeFailure, MediaBridge
from app.calling.prompts import session_config
from app.logging_config import log_event

SECRET_URL = 'https://api.elevenlabs.io/v1/convai/realtime/client_secrets'
SOCKET_URL = 'wss://api.elevenlabs.io/v1/convai/realtime'


def realtime_config(settings, record, selection):
    live = session_config(settings, record)
    backend = live.get('delegation', {}).get('responses', {})
    # The voice policy already owns identity, disclosure and authorization.
    # A separate Responses planner prompt would conflict with a single model's
    # speaking role. Keep its tool schemas, never ask this model to play both roles.
    instructions = live['instructions']
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
Using a custom voice does not make you the owner: never impersonate the owner or claim to be human.
'''
    if record.get('voice_opening_claimed'):
        instructions+='\nThis call already received its opening. Do not greet or introduce yourself again. Continue the existing conversation.'
    return {'type': 'realtime', 'model': selection['model'], 'instructions': instructions,
            'output_modalities': ['audio'], 'tools': backend.get('tools', []),
            'tool_choice': 'auto', 'max_response_output_tokens': 2000,
            'max_duration_seconds': max(60, min(900, int(record['max_duration_minutes'] * 60))),
            'audio': {'input': {'format': {'type': 'audio/pcmu'},
                                'transcription': {'model': 'scribe_v2_realtime'},
                                'turn_detection': {'type': 'semantic_vad', 'eagerness': 'medium',
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
    def __init__(self, socket, config, record, current_record=None, claim_opening=None):
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

    def milliseconds(self):
        return max(0, round((time.monotonic() - self.started_at) * 1000))

    async def send(self, event):
        # Never log messages, ephemeral credentials, transcripts or tool arguments.
        async with self.lock:
            await self.socket.send(json.dumps(event, ensure_ascii=False))

    async def append_note(self, channel, content, event_id):
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
            if event_id:
                key = hashlib.sha256(instructions.encode()).hexdigest()
                self.pending_instructions[key] = (event_id, channel)
            await self.send({'type': 'session.update', 'event_id': event_id or 'note_' + uuid4().hex,
                             'session': {'type': 'realtime', 'instructions': instructions}})
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

    async def request_response(self):
        # Queue a spoken update until the current response finishes. Do not overlap
        # generated turns or cancel a still-running, potentially side-effecting tool.
        if self.active_response or self.response_requested:
            self.want_response = True
            return
        self.want_response = False
        self.response_requested = True
        await self.send({'type': 'response.create', 'response': {'output_modalities': ['audio']}})

    async def interrupt_playback(self, pending_bytes):
        if self.output_item and self.output_bytes:
            played_ms = max(0, (self.output_bytes - pending_bytes) // 8)
            await self.send({'type': 'conversation.item.truncate', 'item_id': self.output_item,
                             'content_index': 0, 'audio_end_ms': played_ms})

    async def close(self):
        self.closing = True
        await self.socket.close()

    def transcript(self, data, speaker, text, item_id):
        start, end = self.speech_times.get(item_id, (self.milliseconds(), self.milliseconds()))
        return {'type': f'session.{speaker}_transcript.delta', 'event_id': data.get('event_id') or uuid4().hex,
                'delta': text, 'start_ms': start, 'end_ms': max(start, end)}

    async def translate(self, data):
        """Yield bridge events only after a real provider event, including update ACKs."""
        kind = data['type']
        if kind == 'session.updated':
            instructions = data.get('session', {}).get('instructions', '')
            key = hashlib.sha256(instructions.encode()).hexdigest()
            if note := self.pending_instructions.pop(key, None):
                yield {'type': 'session.' + note[1] + '.appended', 'client_event_id': note[0]}
        elif kind in ('conversation.item.added', 'conversation.item.created', 'conversation.item.done'):
            item_id = data.get('item', {}).get('id')
            if note := self.pending_notes.pop(item_id, None):
                yield {'type': 'session.' + note[1] + '.appended', 'client_event_id': note[0]}
        elif kind == 'response.created':
            self.active_response = data['response']['id']
            self.response_requested = False
            yield {'type': 'response.event', 'delegation_id': self.active_response, 'event': data}
        elif kind == 'response.output_item.added':
            if data.get('item', {}).get('type') == 'message':
                self.output_item = data['item']['id']
                self.output_bytes = 0
        elif kind == 'response.output_audio.delta':
            if data.get('response_id') not in self.cancelled:
                self.output_bytes += len(base64.b64decode(data['delta'], validate=True))
                yield {'type': 'session.output_audio.delta', 'delta': data['delta']}
        elif kind == 'input_audio_buffer.speech_started':
            self.speech_times[data['item_id']] = (data.get('audio_start_ms', self.milliseconds()), self.milliseconds())
            if self.active_response:
                self.cancelled.add(self.active_response)
            yield {'type': 'session.output_audio.clear'}
        elif kind == 'input_audio_buffer.speech_stopped':
            item_id = data['item_id']
            start = self.speech_times.get(item_id, (self.milliseconds(), 0))[0]
            self.speech_times[item_id] = (start, max(start, data.get('audio_end_ms', self.milliseconds())))
        elif kind in ('conversation.item.input_audio_transcription.delta',
                      'conversation.item.input_audio_transcription.completed'):
            item_id = data['item_id']
            previous = self.input_text.get(item_id, '')
            text = data.get('delta', '') if kind.endswith('.delta') else (
                data.get('transcript', '')[len(previous):] if data.get('transcript', '').startswith(previous) else '')
            self.input_text[item_id] = previous + text
            if text:
                yield self.transcript(data, 'input', text, item_id)
        elif kind in ('response.output_audio_transcript.delta', 'response.output_audio_transcript.done'):
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
        try:
            async for raw in self.socket:
                async for data in self.translate(json.loads(raw)):
                    yield Event(data)
        except ConnectionClosed:
            if not self.closing:
                raise
        if self.closing:
            yield Event({'type': 'session.closed'})
        else:
            raise BridgeFailure('elevenlabs_transport_closed')

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
    if record.get('direction') == 'inbound':
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
        connection = RealtimeConnection(socket, config, record,
                                       current_record=lambda: manager.db.get(record['call_id']),
                                       claim_opening=lambda:manager.voice_backends.claim_opening(record['call_id']))
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
