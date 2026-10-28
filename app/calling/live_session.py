import asyncio
import base64
import math
import re
import traceback
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress

from fastapi import WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI
from twilio.request_validator import RequestValidator
from twilio.base.exceptions import TwilioRestException

from app.calling.prompts import greeting_instruction, session_config
from app.logging_config import log_event
from app.storage.models import TERMINAL
from app.calling.tools import VoiceTools


class BridgeFailure(RuntimeError):
    """Application-owned reason; never contains provider text or conversation data."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def failure_details(exc):
    details = {'reason': exc.reason if isinstance(exc, BridgeFailure) else 'unexpected_error',
               'error_type': type(exc).__name__}
    # Stack locations help diagnose unexpected failures without raw messages,
    # exception reprs, locals, SDK request URLs or transcript contents.
    details['locations'] = [f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(exc.__traceback__)[-4:]]
    close = getattr(exc, 'rcvd', None) or getattr(exc, 'sent', None)
    if isinstance(getattr(close, 'code', None), int):
        details['close_code'] = close.code
    provider = exc if isinstance(exc, TwilioRestException) else exc.__cause__
    if isinstance(provider, TwilioRestException):
        # Numeric codes help diagnose setup without logging URLs, caller data or tokens.
        if isinstance(provider.code, int): details['twilio_error_code'] = provider.code
        if isinstance(provider.status, int): details['twilio_http_status'] = provider.status
    return details


def live_client(settings):
    return AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value(), max_retries=0,
                       default_headers={'User-Agent': 'local-phone-agent/0.1'})


@asynccontextmanager
async def prepared_connection(settings,manager,record):
    """Overlap socket setup with the inbound cap; start no session until both succeed."""
    async with live_client(settings) as client, AsyncExitStack() as stack:
        if record.get('direction')!='inbound':
            yield await stack.enter_async_context(client.live.connect())
            return
        async def cap():
            try:
                await asyncio.wait_for(manager.gateway.prepare_inbound(
                    record['twilio_call_sid'],record['max_duration_minutes']),5)
            except Exception as exc:
                raise BridgeFailure('incoming_duration_limit_failed') from exc
        tasks=[asyncio.create_task(stack.enter_async_context(client.live.connect())),asyncio.create_task(cap())]
        try:
            connection,_=await asyncio.gather(*tasks)
            yield connection
        finally:
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)


def optional_compaction_notice(data: dict) -> bool:
    error = data.get('error', {})
    message = str(error.get('message', ''))
    # A restricted Live-only key can receive this advisory before session.started.
    # Other missing_scope errors remain fatal. Do not broaden key permissions.
    return (error.get('code') == 'missing_scope'
            and message.startswith('History compaction is disabled for this session')
            and 'api.responses.write' in message
            and 'The session will continue' in message)


async def start_session(connection, config: dict) -> dict:
    await connection.session.start(session=config)
    async with asyncio.timeout(10):
        async for event in connection:
            data = event.model_dump()
            if event.type == 'session.started':
                return data
            if event.type == 'error':
                if optional_compaction_notice(data):
                    log_event('gpt_live_notice', feature='history_compaction_disabled')
                    continue
                raise BridgeFailure('live_initialization_rejected')
    raise BridgeFailure('live_closed_before_start')


def valid_stream_start(settings, record: dict, start: dict) -> bool:
    fmt = start.get('mediaFormat', {})
    return (start.get('accountSid') == settings.twilio_account_sid
            and start.get('callSid') == record['twilio_call_sid']
            and bool(start.get('streamSid'))
            and fmt.get('encoding') == 'audio/x-mulaw'
            and fmt.get('sampleRate') == 8000 and fmt.get('channels') == 1)


def mulaw_sample(byte: int) -> int:
    """Decode a G.711 mu-law sample without removed Python audioop APIs."""
    value = byte ^ 0xff
    magnitude = (((value & 0x0f) << 3) + 132) << ((value & 0x70) >> 4)
    return (132 - magnitude) if value & 0x80 else (magnitude - 132)


MULAW_POWER = tuple(mulaw_sample(value) ** 2 for value in range(256))
FAREWELL = re.compile(r'\bthank\s+you[\s.!?,]+goodbye\b|дякую[\s.!?,]+до\s+побачення', re.IGNORECASE)
PLAYBACK_LIMIT_BYTES = 24000  # Three seconds of 8 kHz G.711 audio.
PLAYBACK_CHUNK_BYTES = 800
PLAYBACK_STALL_SECONDS = 3
FINALIZATION_TIMEOUT_SECONDS = 5
GREETING_NUDGE_SECONDS = .35


def has_speech(payload: bytes) -> bool:
    # Only used to finish the instructed goodbye, never for normal turn-taking.
    return bool(payload) and math.sqrt(sum(MULAW_POWER[b] for b in payload) / len(payload)) > 200


class MediaBridge:
    def __init__(self, websocket, connection, manager, call_id, stream_sid, *, luna_booking=False):
        self.ws, self.connection, self.manager = websocket, connection, manager
        self.call_id, self.stream_sid = call_id, stream_sid
        self.finalized = asyncio.Event()
        self.closing = False
        self.pending_marks: dict[str, int] = {}
        self.playback_progress = asyncio.Event()
        self.mark_index = 0
        self.end_requested = asyncio.Event()
        self.end_played = asyncio.Event()
        self.end_mark = 'end_call_playback'
        self.end_requested_at = 0.0
        self.last_speech_at = 0.0
        self.last_transcript_at = 0.0
        self.closing_transcript_seen = False
        self.closing_text = ''
        self.farewell_seen = False
        self.playback_sealed = False
        self.handoff = False
        self.updates_ready = asyncio.Event()
        self.updates_lock = asyncio.Lock()
        self.session_id = manager.db.get(call_id)['openai_session_id'] or stream_sid
        self.inbound = manager.db.get(call_id).get('direction') == 'inbound'
        self.first_audio_logged=False
        self.assistant_spoke=False
        self.caller_spoke=False
        self.greeting_nudged=False
        self.input_speech_frames=0
        self.last_input_speech_at=0.0
        self.startup_started=time.monotonic()
        self.luna_booking=luna_booking
        self.tools = VoiceTools(self)
        previous = manager.db.get(call_id)['transcript']
        self.transcript_offset_ms = int(max((e['end_timestamp'] for e in previous), default=0) * 1000)
        self.continuity_pending = bool(self.inbound and previous)
        self.continuity_sent = False
        self.continuity_event_id = 'call_continuity_' + stream_sid

    def note_conversation_progress(self):
        if self.inbound and self.assistant_spoke and self.caller_spoke and not self.continuity_sent:
            self.continuity_pending = True
            self.updates_ready.set()

    async def send_pending_updates(self):
        async with self.updates_lock:
            record = self.manager.db.get(self.call_id)
            if (self.closing or self.handoff or self.end_requested.is_set()
                    or self.call_id in self.manager.stopping or record['status'] in TERMINAL
                    or self.manager.bridges.get(self.call_id) is not self):
                return
            if self.continuity_pending and not self.continuity_sent:
                from app.calling.inbound import INCOMING_CALL_CONTINUITY
                # One phase update, sent by the existing update worker so audio
                # reception never waits for this optional instruction.
                self.continuity_sent = True
                try:
                    await asyncio.wait_for(self.connection.session.instructions.append(
                        content=INCOMING_CALL_CONTINUITY, delegation_id=None,
                        event_id=self.continuity_event_id), 3)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event('call_continuity_unconfirmed', call_id=self.call_id,
                              error_type=type(exc).__name__)
            for row in record['call_updates']:
                if (self.closing or self.handoff or self.end_requested.is_set()
                        or self.call_id in self.manager.stopping or record['status'] in TERMINAL
                        or self.manager.bridges.get(self.call_id) is not self):
                    return
                if row['status'] != 'queued' or not self.manager.db.claim_update(
                        self.call_id, row['update_id'], self.session_id):
                    continue
                event_id = 'call_update_' + row['update_id']
                channel = {'context': 'thinking', 'say': 'commentary', 'instructions': 'instructions'}[row['mode']]
                content='Owner update within existing policy; no expanded authorization:\n' + row['content']
                if self.inbound and (self.assistant_spoke or self.caller_spoke or record['transcript']):
                    from app.calling.inbound import INCOMING_CALL_CONTINUITY
                    content=INCOMING_CALL_CONTINUITY+'\n'+content
                try:
                    await asyncio.wait_for(getattr(self.connection.session, channel).append(
                        event_id=event_id, delegation_id=None,
                        content=content), 5)
                except asyncio.CancelledError:
                    self.manager.db.finish_update(self.call_id, row['update_id'], self.session_id,
                        'unconfirmed', error_code='send_interrupted')
                    raise
                except Exception as exc:
                    self.manager.db.finish_update(self.call_id, row['update_id'], self.session_id,
                        'unconfirmed', error_code='send_failed')
                    log_event('call_update_unconfirmed', call_id=self.call_id, update_id=row['update_id'],
                              error_type=type(exc).__name__)

    async def pump_updates(self):
        while True:
            self.updates_ready.clear()
            await self.send_pending_updates()
            await self.updates_ready.wait()

    def update_event(self, data):
        event_id = (data.get('error', {}).get('client_event_id') if data['type'] == 'error' else None) or data.get('client_event_id')
        if event_id == self.continuity_event_id:
            if data['type'] == 'error':
                # A rejected optional phase update must not end a healthy call.
                log_event('call_continuity_rejected', call_id=self.call_id)
            return data['type'] in ('error', 'session.instructions.appended')
        if not isinstance(event_id, str) or not event_id.startswith('call_update_'):
            return False
        update_id = event_id.removeprefix('call_update_')
        row = self.manager.db.call_update(self.call_id, update_id)
        if not row or row['openai_session_id'] != self.session_id:
            return False
        expected = 'session.' + {'context':'thinking', 'say':'commentary', 'instructions':'instructions'}[row['mode']] + '.appended'
        if data['type'] == expected:
            self.manager.db.finish_update(self.call_id, update_id, self.session_id, 'acknowledged',
                start_ms=data['start_ms']+self.transcript_offset_ms if data.get('start_ms') is not None else None,
                end_ms=data['end_ms']+self.transcript_offset_ms if data.get('end_ms') is not None else None)
        elif data['type'] == 'error':
            # An invalid append must not tear down an otherwise healthy phone call.
            self.manager.db.finish_update(self.call_id, update_id, self.session_id,
                'rejected', error_code='provider_rejected')
            log_event('call_update_rejected', call_id=self.call_id, update_id=update_id)
        else:
            return False
        return True

    async def request_end_call(self):
        # Recognize only the assistant's application-defined closing phrase.
        # Never run a command based on the recipient's transcript.
        if self.end_requested.is_set():
            return
        self.end_requested_at = asyncio.get_running_loop().time()
        log_event('assistant_end_call_requested', call_id=self.call_id)
        self.end_requested.set()

    async def finish_requested_call(self):
        await self.end_requested.wait()
        loop = asyncio.get_running_loop()
        # There is no Live output-turn-done event. Wait for a closing transcript
        # and a quiet audio tail, then acknowledge actual Twilio playback.
        deadline = loop.time() + 15
        while loop.time() < deadline:
            quiet_since = max(self.end_requested_at, self.last_speech_at, self.last_transcript_at)
            if self.farewell_seen and loop.time() - quiet_since >= 1.0:
                break
            await asyncio.sleep(0.05)
        else:
            log_event('assistant_goodbye_timeout', call_id=self.call_id)
        # Stop enqueuing even silent continuous Live frames before the barrier.
        self.playback_sealed = True
        await self.ws.send_json({'event': 'mark', 'streamSid': self.stream_sid,
                                 'mark': {'name': self.end_mark}})
        try:
            await asyncio.wait_for(self.end_played.wait(), timeout=4)
            log_event('assistant_goodbye_played', call_id=self.call_id)
        except TimeoutError:
            log_event('assistant_playback_unconfirmed', call_id=self.call_id)
        # Returning lets the bridge finalize Live and its owner hang up Twilio.

    async def receive_twilio(self):
        while True:
            message = await self.ws.receive_json()
            if message.get('streamSid') != self.stream_sid:
                raise ValueError('Stream mismatch')
            if message['event'] == 'media':
                if message['media'].get('track') == 'inbound':
                    if self.manager.settings.appointments_enabled:
                        raw=base64.b64decode(message['media']['payload'],validate=True)
                        if has_speech(raw):
                            self.last_input_speech_at=time.monotonic()
                            at=int(message['media'].get('timestamp',0))+self.transcript_offset_ms
                            previous=self.manager.caller_activity.get(self.call_id,{})
                            epoch=previous.get('epoch',0)+(at-previous.get('last_speech_ms',-1000)>400)
                            self.manager.caller_activity[self.call_id]={'epoch':epoch,'last_speech_ms':at}
                    if self.inbound and not self.caller_spoke:
                        speech=has_speech(base64.b64decode(message['media']['payload'],validate=True))
                        self.input_speech_frames=self.input_speech_frames+1 if speech else 0
                        self.caller_spoke=self.input_speech_frames>=3
                    # Forward all codec frames, including silence. GPT-Live is full duplex.
                    await self.connection.session.input_audio.append(audio=message['media']['payload'])
            elif message['event'] == 'mark':
                if self.pending_marks.pop(message['mark']['name'], None) is not None:
                    self.playback_progress.set()
                if self.playback_sealed and message['mark']['name'] == self.end_mark:
                    self.end_played.set()
            elif message['event'] == 'stop':
                log_event('twilio_media_stopped', call_id=self.call_id)
                return

    async def send_live_audio(self, payload: bytes):
        # Provider deltas can arrive in bursts. Bound Twilio's queue while its
        # independent receive task keeps accepting audio and playback ACKs.
        for offset in range(0, len(payload), PLAYBACK_CHUNK_BYTES):
            chunk = payload[offset:offset + PLAYBACK_CHUNK_BYTES]
            while sum(self.pending_marks.values()) + len(chunk) > PLAYBACK_LIMIT_BYTES:
                if self.closing or self.playback_sealed:
                    return
                self.playback_progress.clear()
                try:
                    await asyncio.wait_for(self.playback_progress.wait(), PLAYBACK_STALL_SECONDS)
                except TimeoutError:
                    log_event('twilio_playback_stalled', call_id=self.call_id,
                              pending_bytes=sum(self.pending_marks.values()))
                    raise BridgeFailure('twilio_playback_stalled') from None
            if self.closing or self.playback_sealed:
                return
            name = str(self.mark_index)
            self.mark_index += 1
            self.pending_marks[name] = len(chunk)
            await self.ws.send_json({'event': 'media', 'streamSid': self.stream_sid,
                                     'media': {'payload': base64.b64encode(chunk).decode()}})
            await self.ws.send_json({'event': 'mark', 'streamSid': self.stream_sid,
                                     'mark': {'name': name}})

    async def receive_live(self):
        async for event in self.connection:
            data = event.model_dump()
            if self.update_event(data):
                continue
            if event.type == 'session.output_audio.delta' and not self.closing and not self.playback_sealed:
                payload = base64.b64decode(data['delta'], validate=True)
                if not self.assistant_spoke and has_speech(payload): self.assistant_spoke=True
                if not self.first_audio_logged:
                    self.first_audio_logged=True
                    log_event('gpt_live_first_audio',call_id=self.call_id,
                        startup_ms=round((time.monotonic()-self.startup_started)*1000))
                if self.end_requested.is_set() and has_speech(payload):
                    self.last_speech_at = asyncio.get_running_loop().time()
                await self.send_live_audio(payload)
            elif event.type in ('session.input_transcript.delta', 'session.output_transcript.delta'):
                if data.get('delta','').strip():
                    if event.type=='session.output_transcript.delta': self.assistant_spoke=True
                    else: self.caller_spoke=True
                data = {**data, 'event_id': f"{self.stream_sid}:{data['event_id']}",
                        'start_ms': data['start_ms'] + self.transcript_offset_ms,
                        'end_ms': data['end_ms'] + self.transcript_offset_ms}
                self.manager.db.transcript(self.call_id, data)
                self.note_conversation_progress()
                gateway=getattr(self.manager,'appointments',None)
                if self.inbound and gateway and self.manager.settings.appointments_enabled:
                    gateway.voice_workflow.notify(self,caller=event.type=='session.input_transcript.delta')
                if (event.type == 'session.output_transcript.delta' and data.get('delta')):
                    self.closing_transcript_seen = True
                    self.closing_text = (self.closing_text + data['delta'])[-600:]
                    self.farewell_seen = bool(FAREWELL.search(self.closing_text))
                    self.last_transcript_at = asyncio.get_running_loop().time()
                    if self.farewell_seen:
                        await self.request_end_call()
            elif event.type == 'session.delegation.created':
                if data['delegation']['target'] == 'client':
                    await self.connection.session.commentary.append(
                        delegation_id=data['delegation']['id'],
                        content='No external tools are available. Answer from known information or explain the limitation. To end this call yourself, say Thank you. Goodbye. in English, or Дякую. До побачення. in Ukrainian, then remain silent. Call ending is automatic and needs no delegation.')
            elif event.type == 'response.event':
                if self.manager.settings.live_tools_enabled or self.inbound or self.manager.settings.call_recording_enabled:
                    self.tools.handle(data)
            elif event.type == 'error':
                if optional_compaction_notice(data):
                    log_event('gpt_live_notice', call_id=self.call_id,
                              feature='history_compaction_disabled')
                    continue
                log_event('gpt_live_error', call_id=self.call_id,
                          code=str(data.get('error', {}).get('code', 'unknown')))
                raise BridgeFailure('live_session_error')
            elif event.type == 'session.closed':
                self.finalized.set()
                if not self.handoff:
                    self.manager.db.update(self.call_id, voice_finalized=1)
                log_event('gpt_live_closed', call_id=self.call_id)
                return
        if not self.finalized.is_set():
            raise BridgeFailure('live_transport_closed_without_finalization')

    async def nudge_greeting(self):
        await asyncio.sleep(GREETING_NUDGE_SECONDS)
        if (not self.inbound or self.greeting_nudged or self.assistant_spoke or self.caller_spoke or self.closing
                or self.end_requested.is_set() or self.call_id in self.manager.stopping): return
        from app.calling.inbound import startup_greeting_content
        content=startup_greeting_content(self.manager.settings,self.manager.voice_record(self.call_id))
        if not content: return
        self.greeting_nudged=True
        try:
            await asyncio.wait_for(self.connection.session.commentary.append(content=(
                'One-time startup opening only. If the conversation has already started, do not repeat it. '
                'Otherwise greet once, then listen. Opening: '+content),
                delegation_id=None,event_id='startup_greeting_fallback'),3)
            log_event('gpt_live_greeting_nudged',call_id=self.call_id)
        except Exception as exc:
            log_event('gpt_live_greeting_nudge_unconfirmed',call_id=self.call_id,error_type=type(exc).__name__)

    async def run(self, minutes: int):
        receiver = asyncio.create_task(self.receive_live())
        sender = asyncio.create_task(self.receive_twilio())
        ending = asyncio.create_task(self.finish_requested_call())
        updates = asyncio.create_task(self.pump_updates())
        history=asyncio.create_task(self.manager.lookup.prefetch_history(self.call_id)) if self.inbound else None
        greeting=asyncio.create_task(self.nudge_greeting()) if self.inbound else None
        primary_error = None
        try:
            record = self.manager.db.get(self.call_id)
            if not self.inbound:
                instruction = ('Continue the existing call after keypad navigation. Do not repeat your introduction. '
                           'Listen for a human; stay silent during menus and hold music.' if record['transcript'] else
                           ('Listen first. Stay silent during automated menus; introduce yourself when a human answers.'
                            if record.get('ivr_mode') else greeting_instruction(self.manager.settings)))
                await self.connection.session.instructions.append(
                    content=instruction, delegation_id=None, event_id='initial_greeting')
            done, _ = await asyncio.wait([receiver, sender, ending, updates], timeout=minutes * 60,
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            receiver_failed_before_cleanup = receiver.done() and not receiver.cancelled() and receiver.exception()
            self.closing = True
            sender.cancel()
            ending.cancel()
            updates.cancel()
            if history: history.cancel()
            if greeting: greeting.cancel()
            await asyncio.gather(sender, ending, updates, return_exceptions=True)
            if history: await asyncio.gather(history,return_exceptions=True)
            if greeting: await asyncio.gather(greeting,return_exceptions=True)
            with suppress(Exception):
                await self.ws.send_json({'event': 'clear', 'streamSid': self.stream_sid})
            if not receiver.done():
                try:
                    await self.connection.session.close()
                    await asyncio.wait_for(self.finalized.wait(), timeout=FINALIZATION_TIMEOUT_SECONDS)
                except Exception:
                    log_event('gpt_live_finalization_unconfirmed', call_id=self.call_id)
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            # Preserve the first fault. A late Live close error must not replace
            # a Twilio disconnect or label an already-ended call a bridge failure.
            if receiver.done() and not receiver.cancelled() and receiver.exception():
                if receiver_failed_before_cleanup and primary_error is None:
                    raise receiver.exception()
                if not receiver_failed_before_cleanup:
                    log_event('gpt_live_finalization_failed', call_id=self.call_id,
                              **failure_details(receiver.exception()))


async def handle_media(websocket: WebSocket, call_id: str):
    manager = websocket.app.state.manager
    settings = manager.settings
    token = settings.twilio_auth_token.get_secret_value()
    # Validate only this configured origin/path. Media Streams can sign the WSS
    # URL; Twilio also documents a trailing-slash handshake variation.
    # Never derive the trusted origin from proxy/request headers.
    urls = [settings.public_url(websocket.url.path, websocket=use_wss) + suffix
            for use_wss in (True, False) for suffix in ('', '/')]
    signature = websocket.headers.get('x-twilio-signature', '')
    matched = next((url for url in urls if token and
                    RequestValidator(token).validate(url, {}, signature)), None)
    if not matched:
        log_event('twilio_media_rejected', call_id=call_id, reason='signature')
        await websocket.close(code=1008)
        return
    log_event('twilio_media_authenticated', call_id=call_id,
              scheme='wss' if matched.startswith('wss:') else 'https',
              trailing_slash=matched.endswith('/'))
    try:
        record = manager.db.get(call_id)
    except KeyError:
        await websocket.close(code=1008)
        return
    if record['status'] in TERMINAL or not record['twilio_call_sid']:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    bound = False
    claimed = False
    bridge = None
    try:
        async with asyncio.timeout(10):
            while True:
                message = await websocket.receive_json()
                if message['event'] == 'start':
                    if not valid_stream_start(settings, record, message['start']):
                        await websocket.close(code=1008)
                        return
                    previous=manager.bridges.get(call_id)
                    if call_id in manager.connecting or (previous and not previous.handoff):
                        await websocket.close(code=1008)
                        return
                    manager.connecting.add(call_id)
                    claimed=True
                    bound = True
                    stream_sid = message['start']['streamSid']
                    break
                if message['event'] != 'connected':
                    raise ValueError('Expected Twilio start')
        manager.transition(call_id, 'in_progress')
        startup_started=time.monotonic()
        from app.calling.transfer import remaining_seconds
        if remaining_seconds(manager.db.get(call_id))<=0:
            raise BridgeFailure('call_duration_exhausted')
        async with prepared_connection(settings,manager,record) as connection:
            record = manager.voice_record(call_id)
            started = await start_session(connection, session_config(settings, record))
            manager.db.restore_updates(call_id, record['call_updates'])
            manager.db.update(call_id, openai_session_id=started['session']['id'], voice_finalized=0)
            log_event('gpt_live_connected', call_id=call_id)
            bridge = MediaBridge(websocket, connection, manager, call_id, stream_sid,luna_booking=True)
            bridge.startup_started=startup_started
            manager.attach_bridge(call_id, bridge)
            manager.connecting.discard(call_id)
            claimed=False
            from app.calling.transfer import remaining_seconds
            await bridge.run(remaining_seconds(record)/60)
    except WebSocketDisconnect as exc:
        log_event('twilio_media_disconnected', call_id=call_id, close_code=exc.code)
        if bound and not (bridge and bridge.handoff) and exc.code not in (1000, 1001):
            manager.db.voice_error(call_id, 'Twilio audio connection lost',
                {'reason':'twilio_media_disconnected', 'close_code':exc.code})
            manager.transition(call_id, 'failed')
    except Exception as exc:
        if bound and not (bridge and bridge.handoff):
            details = failure_details(exc)
            manager.db.voice_error(call_id,
                f"Voice bridge failed: {details['reason']} ({details['error_type']})", details)
            manager.transition(call_id, 'failed')
        log_event('voice_bridge_failed', call_id=call_id, **failure_details(exc))
    finally:
        if claimed: manager.connecting.discard(call_id)
        # Ensure a stopped/broken bridge cannot leave a paid silent call running.
        if bridge:
            manager.detach_bridge(call_id, bridge)
        if bound and not (bridge and bridge.handoff):
            try:
                manager.capture_inbound_fallback(call_id)
            except Exception as exc:
                log_event('incoming_message_capture_failed', call_id=call_id, error_type=type(exc).__name__)
            try:
                await manager.gateway.hangup(record['twilio_call_sid'])
            except Exception as exc:
                log_event('twilio_hangup_unconfirmed', call_id=call_id, error_type=type(exc).__name__)
        with suppress(Exception):
            await websocket.close()
