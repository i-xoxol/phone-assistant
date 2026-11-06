"""Owner-run browser calls using the production media bridge, without a phone leg."""
import asyncio
import base64
import hashlib
import secrets
import time
from contextlib import suppress
from typing import Literal
from uuid import UUID, uuid4

from fastapi import WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.storage.models import TERMINAL
from app.calling.context import utcnow


class BrowserCall(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    scenario: Literal['direct', 'forwarded', 'outbound'] = 'direct'
    phone_number: str = Field(default='', pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    forwarded_from: str = Field(default='', pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    recipient: str = Field(default='', max_length=200)
    objective: str = Field(default='', max_length=4000)
    context: str = Field(default='', max_length=8000)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    max_duration_minutes: int = Field(default=5, ge=1, le=15)
    ivr_mode: bool = False
    caller_id: Literal['default', 'twilio', 'mobile'] = 'default'
    personality_id: UUID | None = None
    voice_provider: Literal['openai', 'elevenlabs', 'gemini'] | None = None
    voice_model: str | None = Field(default=None, max_length=100)
    voice: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def scenario_fields(self):
        if self.scenario == 'forwarded' and not self.forwarded_from:
            raise ValueError('Choose the number this incoming call was forwarded from')
        if self.scenario != 'forwarded' and self.forwarded_from:
            raise ValueError('Forwarding metadata belongs to a forwarded incoming test')
        if self.scenario == 'outbound' and (not self.phone_number or not self.objective.strip()):
            raise ValueError('Outgoing tests require a recipient number and objective')
        if not self.voice_provider and (self.voice_model or self.voice):
            raise ValueError('Choose a provider with a model or voice override')
        return self


class BrowserTests:
    def __init__(self, manager):
        self.m, self.db = manager, manager.db
        self.pending = {}
        with self.db.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS browser_test_calls (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id), request_id TEXT UNIQUE NOT NULL,
                owner_hash TEXT NOT NULL, fingerprint TEXT NOT NULL, scenario TEXT NOT NULL,
                created_at REAL NOT NULL, connected INTEGER NOT NULL DEFAULT 0,
                closed_announcement TEXT NOT NULL DEFAULT '', closed_end INTEGER NOT NULL DEFAULT 0)''')
            # A browser audio session cannot survive a process restart.
            stale = [r[0] for r in db.execute("SELECT call_id FROM browser_test_calls JOIN calls USING(call_id) "
                "WHERE status NOT IN ('completed','failed','busy','no_answer','cancelled')")]
        for cid in stale:
            self.m.transition(cid, 'cancelled')

    def get(self, cid):
        with self.db.connect() as db:
            row = db.execute('SELECT scenario,created_at,connected,closed_announcement,closed_end '
                'FROM browser_test_calls WHERE call_id=?', (cid,)).fetchone()
        return dict(row) if row else None

    def recent_numbers(self):
        import re
        from app.storage.models import E164
        with self.db.connect() as db:
            rows = list(db.execute('''SELECT c.phone_number,c.created_at,c.call_id,r.forwarded_from
                FROM calls c LEFT JOIN incoming_routing r USING(call_id)
                WHERE NOT EXISTS (SELECT 1 FROM browser_test_calls b WHERE b.call_id=c.call_id)
                ORDER BY c.created_at DESC,c.call_id DESC LIMIT 100'''))
            contacts = list(db.execute('SELECT name,phone_number FROM concierge_contacts'))
        names = {}
        for row in contacts:
            names.setdefault(row['phone_number'], []).append(row['name'])
        groups = {'forwarding': {}, 'calls': {}}
        for row in rows:
            for group, field in [('forwarding','forwarded_from'), ('calls','phone_number')]:
                number = row[field] or ''
                if re.fullmatch(E164, number) and number not in groups[group]:
                    groups[group][number] = {'number':number, 'last_seen':row['created_at'],
                        'contact_name':names[number][0] if len(names.get(number, [])) == 1 else ''}
        return {'forwarding_sources':list(groups['forwarding'].values())[:20],
            'call_numbers':[row for number,row in groups['calls'].items() if number not in groups['forwarding']][:20]}

    def create(self, body, csrf):
        fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
        owner_hash = hashlib.sha256(csrf.encode()).hexdigest()
        with self.db.connect() as db:
            old = db.execute('SELECT * FROM browser_test_calls WHERE request_id=?', (str(body.request_id),)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint or old['owner_hash'] != owner_hash:
                raise ValueError('This test request ID is already in use')
            return self.m.result(old['call_id'])
        inbound = body.scenario != 'outbound'
        closed = not self.m.hours.state()['office_open']
        hours = self.m.hours.policy()
        mode = 'messages' if closed and hours.outside_hours == 'messages' else self.m.settings.inbound_mode
        route = self.m.rules.evaluate({'phone_number': body.phone_number, 'call_id': '',
            'direction': 'inbound' if inbound else 'outbound', '_preview_forwarding': {
                'detected': bool(body.forwarded_from), 'forwarded_from': body.forwarded_from,
                'caller_identity_ambiguous': bool(body.forwarded_from and body.forwarded_from == body.phone_number)}})
        planned = {'call_id':'', 'phone_number': body.phone_number,
            'direction': 'inbound' if inbound else 'outbound', 'routing_rule': route,
            '_preview_forwarding': {'caller_identity_ambiguous':bool(body.forwarded_from and body.forwarded_from == body.phone_number)}}
        self.m.voice_backends.for_record(planned, body.voice_provider, body.voice_model, body.voice)
        if body.personality_id:
            self.m.personalities.get(body.personality_id)
        cid, stamp = str(uuid4()), utcnow().isoformat()
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM browser_test_calls JOIN calls USING(call_id) "
                    "WHERE status NOT IN ('completed','failed','busy','no_answer','cancelled')").fetchone():
                raise ValueError('End the current browser test before starting another')
            db.execute('''INSERT INTO calls(call_id,created_at,phone_number,recipient,objective,context,
                max_duration_minutes,status,direction,inbound_mode,inbound_booking_allowed,ivr_mode)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', (cid, stamp, body.phone_number,
                'Browser test · ' + (body.recipient or ('incoming' if inbound else 'outgoing')),
                ('Help the caller through the trusted supervisor or take a message.' if mode == 'concierge' else 'Take a message for the owner.')
                    if inbound else body.objective.strip(),
                ('Unverified incoming caller. Private lookups and appointment actions require the trusted supervisor.'
                    if mode == 'concierge' else 'Unverified incoming caller. Message-taking only; no private lookups or actions.')
                    if inbound else body.context, body.max_duration_minutes,
                'ringing', 'inbound' if inbound else 'outbound', mode if inbound else 'messages',
                int(inbound and self.m.settings.inbound_booking_allowed and not closed), int(body.ivr_mode)))
            db.executemany('INSERT INTO constraints VALUES (?,?,?)', [(cid, i, text) for i, text in enumerate(body.constraints)])
            db.execute('INSERT INTO browser_test_calls VALUES (?,?,?,?,?,?,0,?,?)',
                (cid, str(body.request_id), owner_hash, fingerprint, body.scenario, time.time(),
                 self.m.hours.closed_announcement() if inbound and closed else '',
                 int(inbound and closed and hours.outside_hours == 'closed')))
            if not inbound:
                choice = body.caller_id
                if choice == 'default':
                    choice = route.get('actions', {}).get('caller_id') or self.m.outbound.get()['defaults']['caller_id']
                number = self.m.settings.twilio_phone_number if choice == 'twilio' else self.m.outbound.mobile()
                db.execute('INSERT INTO call_origination VALUES (?,?,?,?,?)', (cid, None, '', choice, number))
            self.db._event(db, cid, 'call.created', {'status': 'ringing', 'direction': planned['direction'],
                'recipient': 'Browser test', 'browser_test': True})
            if inbound:
                self.db._event(db, cid, 'call.incoming', {'direction':'inbound', 'mode':mode,
                    'booking_allowed':bool(self.m.settings.inbound_booking_allowed and not closed), 'browser_test':True})
        try:
            self.m.routing.save(cid, {'ForwardedFrom': body.forwarded_from})
            record = self.db.get(cid)
            record['routing_rule'] = self.m.rules.pin(record, route)
            self.m.disclosures.pin(record)
            self.m.personalities.pin(record, body.personality_id)
            self.m.voices.pin(record)
            self.m.voice_backends.pin(record, body.voice_provider, body.voice_model, body.voice)
            self.m.recordings.pin(cid)
            if inbound and not (closed and hours.outside_hours == 'closed'):
                self.m.greetings.start(cid)
            task = asyncio.create_task(self.expire(cid))
            self.pending[cid] = task
            task.add_done_callback(lambda _: self.pending.pop(cid, None))
        except Exception:
            self.m.transition(cid, 'failed')
            raise
        self.db.on_event()
        return self.m.result(cid)

    def finish(self, cid, status='completed'):
        from datetime import datetime
        record = self.db.get(cid)
        duration = max(0, int((utcnow() - datetime.fromisoformat(record['started_at'])).total_seconds())) if record['started_at'] else 0
        self.m.transition(cid, status, duration=duration)

    async def expire(self, cid):
        await asyncio.sleep(30)
        if self.get(cid) and not self.get(cid)['connected'] and self.db.get(cid)['status'] not in TERMINAL:
            self.m.transition(cid, 'cancelled')

    def claim(self, cid, csrf):
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM browser_test_calls JOIN calls USING(call_id) WHERE call_id=?', (cid,)).fetchone()
            if (not row or row['connected'] or row['status'] in TERMINAL or time.time() - row['created_at'] > 30
                    or not secrets.compare_digest(row['owner_hash'], hashlib.sha256(csrf.encode()).hexdigest())):
                raise ValueError('Browser test expired, connected, or belongs to another browser')
            db.execute('UPDATE browser_test_calls SET connected=1 WHERE call_id=?', (cid,))

    async def stop(self):
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class BrowserSocket:
    """Bounded browser packets plus session revocation checks for the shared bridge."""
    def __init__(self, ws, authorize, stream_sid):
        self.ws, self.authorize, self.stream_sid = ws, authorize, stream_sid

    async def send_json(self, data):
        await self.ws.send_json(data)

    async def receive_json(self):
        self.authorize()
        message = await self.ws.receive_json()
        if message.get('event') == 'media':
            payload = message.get('media', {}).get('payload', '')
            if not isinstance(payload, str) or len(payload) > 2200:
                raise ValueError('Invalid browser audio frame')
            raw = base64.b64decode(payload, validate=True)
            timestamp = message.get('media', {}).get('timestamp', 0)
            if not 1 <= len(raw) <= 1600 or not isinstance(timestamp, (int, float)) or not 0 <= timestamp <= 900000:
                raise ValueError('Invalid browser audio frame')
            message['media']['track'] = 'inbound'
        elif message.get('event') == 'mark':
            name = message.get('mark', {}).get('name', '')
            if not isinstance(name, str) or len(name) > 100:
                raise ValueError('Invalid playback acknowledgment')
        elif message.get('event') != 'stop':
            raise ValueError('Unsupported browser audio packet')
        return {**message, 'streamSid': self.stream_sid}


async def handle_browser(ws, cid):
    from app.api.live import owner, same_origin
    from app.calling.live_session import connected_bridge, failure_details
    manager = ws.app.state.manager
    try:
        same_origin(ws)
        session = owner(ws)
        if not manager.browser_tests.get(cid):
            raise ValueError('Not a browser test')
    except Exception:
        await ws.close(code=1008)
        return
    await ws.accept()
    claimed = False
    try:
        async with asyncio.timeout(10):
            message = await ws.receive_json()
            if message.get('event') != 'start' or not secrets.compare_digest(str(message.get('csrf', '')), session['csrf']):
                raise ValueError('Browser authorization required')
        manager.browser_tests.claim(cid, session['csrf'])
        claimed = True
        test = manager.browser_tests.get(cid)
        if test['closed_announcement']:
            await ws.send_json({'event':'test_announcement', 'text':test['closed_announcement'], 'ending':bool(test['closed_end'])})
        if test['closed_end']:
            manager.browser_tests.finish(cid)
            return
        stream_sid = 'browser_' + cid
        socket = BrowserSocket(ws, lambda: owner(ws), stream_sid)
        async with connected_bridge(socket, manager, cid, stream_sid) as bridge:
            await ws.send_json({'event': 'ready', 'call_id': cid})
            await bridge.run(manager.db.get(cid)['max_duration_minutes'])
        manager.browser_tests.finish(cid)
    except WebSocketDisconnect:
        if claimed:
            manager.browser_tests.finish(cid)
    except Exception as exc:
        if claimed:
            manager.db.voice_error(cid, 'Browser voice test failed', failure_details(exc))
            manager.browser_tests.finish(cid, 'failed')
        with suppress(Exception):
            await ws.send_json({'event': 'test_error', 'message': 'Browser test could not continue. Inspect the call status and try a new test.'})
    finally:
        if claimed:
            manager.capture_inbound_fallback(cid)
        with suppress(Exception):
            await ws.close()
