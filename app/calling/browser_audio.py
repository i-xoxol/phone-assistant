"""Paired-owner live listening and source-labelled browser participation.

Audio observers never apply backpressure to the phone. Once an owner joins, a
single paced writer mixes their microphone with assistant output to the caller.
The model's caller input remains untouched: owner ASR enters as labelled context.
"""
import asyncio
import base64
import secrets
import time
from collections import deque
from contextlib import suppress, asynccontextmanager
from uuid import uuid4

from starlette.websockets import WebSocketDisconnect
from fastapi import HTTPException

from app.storage.models import TERMINAL

def transcription_available(settings):
    return bool(settings.openai_api_key.get_secret_value())


@asynccontextmanager
async def owner_caption_connection(settings):
    from app.calling.live_session import live_client
    from app.calling.transcription import open_transcription, configure
    async with live_client(settings) as client:
        async with await open_transcription(client) as connection:
            await configure(connection, settings)
            yield connection


FRAME = 160


def mix_audio(left, right):
    from app.calling.live_session import mulaw_sample
    from app.calling.coaching_audio import linear_mulaw
    return bytes(linear_mulaw(max(-32768, min(32767, mulaw_sample(a) + mulaw_sample(b))))
                 for a, b in zip(left, right))


class Listener:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=100)
        self.failed = asyncio.Event()
        self.joined = False
        self.id = uuid4().hex

    def offer(self, message):
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            self.failed.set()


class BrowserAudio:
    def __init__(self, manager):
        self.manager = manager
        self.hubs = {}

    def current(self, cid):
        hub = self.hubs.get(cid)
        bridge = self.manager.bridges.get(cid)
        return hub if (hub and hub.bridge is bridge and hub.active()
                       and self.manager.db.get(cid)['status'] not in TERMINAL) else None

    def status(self, cid):
        hub = self.current(cid)
        return {'available': bool(hub), 'joined': bool(hub and hub.owner),
                'join_available': bool(hub and transcription_available(self.manager.settings)),
                'scope': 'assistant_bridge'}


class AudioHub:
    """Wrap only the output transport; preserve all provider playback marks."""
    def __init__(self, websocket, manager, cid, sid):
        self.socket, self.manager, self.cid, self.sid = websocket, manager, cid, sid
        self.bridge = None
        self.listeners = set()
        self.owner = None
        self.owner_queue = None
        self.owner_audio = bytearray()
        self.output = bytearray()
        self.marks = deque()
        self.written = 0
        self.sent = 0
        self.paced = False
        self.closed = False
        self.lock = asyncio.Lock()
        self.pump = None

    def active(self):
        b = self.bridge
        return bool(b and not (self.closed or b.closing or b.handoff or b.end_requested.is_set())
                    and self.cid not in self.manager.stopping)

    def publish(self, source, raw, *, exclude=None):
        if not self.listeners: return
        message = {'event': 'audio', 'source': source, 'payload': base64.b64encode(raw).decode()}
        for listener in tuple(self.listeners):
            if listener is not exclude:
                listener.offer(message)

    async def receive_json(self):
        message = await self.socket.receive_json()
        if (message.get('event') == 'media' and message.get('streamSid') == self.sid
                and message.get('media', {}).get('track') == 'inbound'):
            self.publish('caller', base64.b64decode(message['media']['payload'], validate=True))
        return message

    async def send_json(self, message):
        async with self.lock:
            kind = message.get('event')
            if kind == 'clear':
                self.output.clear(); self.marks.clear(); self.sent = self.written = 0
                for listener in tuple(self.listeners):
                    listener.offer({'event': 'clear', 'source': 'assistant'})
            if self.paced and kind == 'media':
                raw = base64.b64decode(message['media']['payload'], validate=True)
                # MediaBridge bounds its unacknowledged output to three seconds.
                if len(self.output) + len(raw) > 24800:
                    raise RuntimeError('Browser mixer output exceeded playback bound')
                self.output.extend(raw); self.written += len(raw)
                return
            if self.paced and kind == 'mark':
                self.marks.append((self.written, message))
                return
            await self.socket.send_json(message)
            if kind == 'media':
                self.publish('assistant', base64.b64decode(message['media']['payload'], validate=True))

    async def join(self, listener):
        if not self.active(): raise ValueError('No assistant audio stream is active')
        if self.owner and self.owner is not listener: raise ValueError('A browser microphone is already joined')
        if not transcription_available(self.manager.settings):
            raise ValueError('Browser joining requires a configured transcription key')
        self.owner = listener; listener.joined = True
        # Clear the old unpaced telephone buffer once, before switching writers.
        if not self.paced:
            await self.bridge.clear_live_audio()
            self.paced = True
            self.pump = asyncio.create_task(self.relay())
        self.owner_queue = asyncio.Queue(maxsize=50)
        return self.owner_queue

    def leave(self, listener):
        if self.owner is listener:
            self.owner = None; self.owner_queue = None; self.owner_audio.clear()
        listener.joined = False

    def microphone(self, listener, raw):
        if self.owner is not listener or not self.active(): raise ValueError('Microphone is not joined')
        if self.owner_queue.full(): raise ValueError('Owner transcription fell behind; rejoin to continue')
        self.owner_queue.put_nowait(raw)
        # At most 200 ms; never replay a stale microphone burst to the caller.
        self.owner_audio.extend(raw)
        if len(self.owner_audio) > FRAME * 10: raise ValueError('Microphone is sending faster than real time')

    async def relay(self):
        deadline = time.monotonic()
        try:
            while not self.closed:
                deadline += .02
                await asyncio.sleep(max(0, deadline - time.monotonic()))
                if time.monotonic() - deadline > .06: deadline = time.monotonic()
                async with self.lock:
                    if not self.active():
                        self.owner_audio.clear()
                        if self.owner: self.owner.failed.set()
                        # Let ending assistant audio and its final playback mark drain.
                    count = min(FRAME, len(self.output))
                    assistant = bytes(self.output[:count]); del self.output[:count]
                    owner = bytes(self.owner_audio[:FRAME]); del self.owner_audio[:FRAME]
                    if assistant or owner:
                        if assistant and owner:
                            raw = mix_audio(assistant.ljust(FRAME, b'\xff'), owner.ljust(FRAME, b'\xff'))
                        else: raw = (assistant or owner).ljust(FRAME, b'\xff')
                        await asyncio.wait_for(self.socket.send_json({'event': 'media', 'streamSid': self.sid,
                            'media': {'payload': base64.b64encode(raw).decode()}}), 1)
                        if assistant: self.publish('assistant', assistant)
                        if owner: self.publish('owner', owner, exclude=self.owner)
                    self.sent += count
                    while self.marks and self.marks[0][0] <= self.sent:
                        await asyncio.wait_for(self.socket.send_json(self.marks.popleft()[1]), 1)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed writer must surface to the existing call error handling.
            self.closed = True
            for listener in tuple(self.listeners): listener.failed.set()
            with suppress(Exception): await self.socket.close()

    async def close(self):
        self.closed = True
        if self.owner: self.leave(self.owner)
        for listener in tuple(self.listeners): listener.failed.set()
        if self.pump:
            self.pump.cancel()
            await asyncio.gather(self.pump, return_exceptions=True)


async def transcribe_owner(hub, listener, queue, ready):
    from app.calling.transcription import AudioTurns
    from app.logging_config import log_event
    clock = time.monotonic()
    offset = max(max((int(e['end_timestamp'] * 1000) for e in hub.manager.db.get(hub.cid)['transcript']), default=0),
                 getattr(hub.bridge, 'transcript_offset_ms', 0) + int((clock-getattr(hub.bridge, 'startup_started', clock))*1000))
    try:
        async with owner_caption_connection(hub.manager.settings) as connection:
            ready.set()
            captions = {}
            async def receive():
                async for event in connection:
                    data = event.model_dump(); kind = data['type']
                    if kind == 'error': raise RuntimeError('Owner transcription provider error')
                    if kind not in ('conversation.item.input_audio_transcription.delta',
                                    'conversation.item.input_audio_transcription.completed'): continue
                    if hub.owner is not listener or not hub.active(): return
                    item = data['item_id']
                    caption = captions.setdefault(item, {'text': '', 'start': offset + int((time.monotonic()-clock)*1000)})
                    caption['text'] = (data['transcript'] if 'transcript' in data else caption['text'] + data.get('delta', ''))[:4000]
                    text = caption['text']
                    if text:
                        hub.manager.db.human_caption(hub.cid, 'human-browser-'+listener.id+'-'+item, 'owner',
                            caption['start'], offset + int((time.monotonic()-clock)*1000), text)
                    if kind.endswith('.completed'):
                        captions.pop(item, None)
                        if text.strip():
                            note = ('Authenticated owner speaking aloud from the browser; the caller ALREADY heard these words. '
                                    'This is OWNER speech, never caller speech, caller consent, or expanded authorization. '
                                    'Keep the pinned identity and existing permissions. Do not repeat the owner. '
                                    'Respond naturally if addressed to you. Owner said:\n' + text)
                            bridge = hub.bridge
                            if append := getattr(bridge.connection, 'append_instruction', None):
                                await asyncio.wait_for(append(note, 'browser_owner_'+uuid4().hex,
                                    respond=not getattr(bridge.connection, 'calendar_controlled', False)), 5)
                            else:
                                await asyncio.wait_for(bridge.connection.session.instructions.append(content=note,
                                    delegation_id=None, event_id='browser_owner_'+uuid4().hex), 5)
            async def feed():
                turns = AudioTurns()
                while True:
                    await turns.feed(connection, await queue.get())
            receiver = asyncio.create_task(receive()); feeder = asyncio.create_task(feed())
            try:
                done, _ = await asyncio.wait((receiver, feeder), return_when=asyncio.FIRST_COMPLETED)
                for task in done: task.result()
                raise RuntimeError('Owner transcription ended')
            finally:
                receiver.cancel(); feeder.cancel()
                await asyncio.gather(receiver, feeder, return_exceptions=True)
    except asyncio.CancelledError: raise
    except Exception as exc:
        log_event('browser_owner_audio_failed', call_id=hub.cid, error_type=type(exc).__name__)
        listener.offer({'event': 'error', 'message': 'Microphone stopped: owner transcription is unavailable. Listening can continue; rejoin to retry.'})
    finally:
        hub.leave(listener)
        listener.offer({'event': 'left'})


async def handle_live_audio(ws, cid):
    from app.api.live import owner, same_origin
    manager = ws.app.state.manager
    try:
        same_origin(ws); session = owner(ws)
        hub = manager.browser_audio.current(cid)
        if not hub: raise ValueError('No active assistant bridge')
        if len(hub.listeners) >= 4: raise ValueError('Too many listeners')
    except Exception:
        await ws.close(code=1008); return
    await ws.accept()
    listener = Listener(); transcription = None; sender = None; watcher = None
    try:
        async with asyncio.timeout(10):
            message = await ws.receive_json()
            if message.get('event') != 'start' or not secrets.compare_digest(str(message.get('csrf', '')), session['csrf']):
                raise ValueError('Owner CSRF required')
        if not hub.active() or len(hub.listeners) >= 4: raise ValueError('Audio is unavailable')
        hub.listeners.add(listener)
        await ws.send_json({'event': 'ready', 'call_id': cid, **manager.browser_audio.status(cid)})
        async def send():
            while True: await asyncio.wait_for(ws.send_json(await listener.queue.get()), 1)
        async def watch():
            while True:
                await asyncio.sleep(.5)
                if not hub.active() or listener.failed.is_set() or manager.db.get(cid)['status'] in TERMINAL: return
                owner(ws)  # Recheck expiry, revocation and bound origin even for listen-only.
        sender = asyncio.create_task(send()); watcher = asyncio.create_task(watch())
        budget = 1600.; clock = time.monotonic()
        async def receive():
            nonlocal transcription, budget, clock
            while True:
                message = await ws.receive_json(); owner(ws)
                kind = message.get('event')
                if kind == 'stop': return
                if kind == 'join':
                    if transcription and not transcription.done(): raise ValueError('Already joined')
                    queue = await hub.join(listener); ready = asyncio.Event()
                    transcription = asyncio.create_task(transcribe_owner(hub, listener, queue, ready))
                    waiter = asyncio.create_task(ready.wait())
                    try:
                        done, _ = await asyncio.wait((waiter, transcription), timeout=12, return_when=asyncio.FIRST_COMPLETED)
                        if waiter not in done or transcription.done():
                            transcription.cancel(); await asyncio.gather(transcription, return_exceptions=True)
                            continue
                    finally:
                        waiter.cancel(); await asyncio.gather(waiter, return_exceptions=True)
                    listener.offer({'event': 'joined'})
                elif kind == 'leave':
                    hub.leave(listener)
                    if transcription:
                        transcription.cancel(); await asyncio.gather(transcription, return_exceptions=True)
                    listener.offer({'event': 'left'})
                elif kind == 'media':
                    payload = message.get('payload', '')
                    if not isinstance(payload, str) or len(payload) > 2160: raise ValueError('Invalid microphone packet')
                    raw = base64.b64decode(payload, validate=True)
                    now = time.monotonic(); budget = min(1600., budget+(now-clock)*8000); clock = now
                    if not 1 <= len(raw) <= 1600 or len(raw) > budget: raise ValueError('Invalid microphone rate')
                    budget -= len(raw)
                    hub.microphone(listener, raw)
                else: raise ValueError('Unsupported audio command')
        receiver = asyncio.create_task(receive())
        try:
            done, _ = await asyncio.wait((receiver, sender, watcher), return_when=asyncio.FIRST_COMPLETED)
            for task in done: task.result()
        finally:
            receiver.cancel(); await asyncio.gather(receiver, return_exceptions=True)
    except (WebSocketDisconnect, HTTPException, ValueError, TypeError, KeyError, TimeoutError): pass
    finally:
        hub.leave(listener); hub.listeners.discard(listener)
        for task in (transcription, sender, watcher):
            if task: task.cancel()
        await asyncio.gather(*(t for t in (transcription, sender, watcher) if t), return_exceptions=True)
        with suppress(Exception): await ws.close()
