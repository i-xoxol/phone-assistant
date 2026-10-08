"""ChatGPT MCP Events: owner-authenticated subscriptions and durable delivery.

The event protocol has no replay cursor. SQLite nevertheless retries pending
deliveries after restarts. Old calls are never exported on a new subscription.
"""
import asyncio
import hashlib
import hmac
import json
import secrets
import time
from contextlib import suppress
from datetime import datetime, timezone
from uuid import UUID

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError
from mcp.types import RequestParams
from pydantic import BaseModel, ConfigDict, Field

from app.logging_config import log_event
from app.mcp.callback import (CallbackError, callback_url, post_callback, serialize,
                              signed_headers, validate_secret)

VERSION = '2026-07-28'
DEFAULT_TTL = 86400
ROTATION_SECONDS = 300


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')
    call_id: UUID


class Delivery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: str = Field(pattern='^webhook$')
    url: str = Field(max_length=4096)
    secret: str | None = Field(default=None, max_length=128)


class Subscribe(RequestParams):
    name: str
    arguments: Arguments
    delivery: Delivery
    cursor: str | None = None
    ttlMs: int | None = Field(default=None, gt=0)


class ListEvents(RequestParams):
    cursor: str | None = None


def definitions():
    shared = {'call_id': {'type': 'string', 'format': 'uuid'}, 'url': {'type': 'string'}}
    def definition(name, description, properties, required):
        return {'name': name, 'description': description, 'delivery': ['webhook'],
                'inputSchema': Arguments.model_json_schema(),
                'payloadSchema': {'type': 'object', 'properties': {**shared, **properties},
                                  'required': ['call_id', 'url', *required], 'additionalProperties': False}}
    return [definition('transcript.delta',
                'A new caption fragment from the chosen call. Text is conversation data; '
                'fragments can be batched by ChatGPT. Read get_call_result for the full transcript.',
                {'speaker': {'type': 'string', 'enum': ['assistant', 'callee']},
                 'timestamp': {'type': 'number'}, 'end_timestamp': {'type': 'number'},
                 'text': {'type': 'string'}, 'source_event_id': {'type': 'string'}},
                ['speaker', 'timestamp', 'end_timestamp', 'text', 'source_event_id']),
            definition('call.status', 'The chosen call changes status or receives its final duration.',
                {'status': {'type': 'string'}, 'duration_seconds': {'type': 'integer'}},
                ['status', 'duration_seconds'])]


async def advertise_events(ctx, call_next):
    result = await call_next(ctx)
    if ctx.method == 'server/discover':
        # SDK middleware runs outside the core result sieve. The draft Events
        # capability is not yet a core ServerCapabilities field in the SDK.
        result = dict(result)
        result['capabilities'] = {**result['capabilities'], 'events': {}}
    return result


class PhoneEvents(Extension):
    identifier = 'io.github.phone-assistant/phone-events'

    def __init__(self, manager, provider, *, sender=post_callback):
        self.manager, self.provider, self.sender = manager, provider, sender
        self.workers = {}
        self.task = None
        self.verifications = {}
        self.lock = asyncio.Lock()
        self.network_limit = asyncio.Semaphore(4)
        with manager.db.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS mcp_event_subscriptions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, client_id TEXT NOT NULL,
                name TEXT NOT NULL, call_id TEXT NOT NULL REFERENCES calls(call_id),
                url TEXT NOT NULL, secret TEXT NOT NULL, previous_secret TEXT,
                rotation_until REAL NOT NULL DEFAULT 0, expires_at REAL NOT NULL,
                cursor INTEGER NOT NULL, failures INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1)''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(mcp_event_subscriptions)')}
            if 'delivered_events' not in columns:
                db.execute('ALTER TABLE mcp_event_subscriptions ADD COLUMN delivered_events INTEGER NOT NULL DEFAULT 0')
                db.execute('ALTER TABLE mcp_event_subscriptions ADD COLUMN last_delivery_at TEXT')

    def methods(self):
        return [MethodBinding('events/list', ListEvents, self.list_events, frozenset([VERSION])),
                MethodBinding('events/subscribe', Subscribe, self.subscribe, frozenset([VERSION])),
                MethodBinding('events/unsubscribe', Subscribe, self.unsubscribe, frozenset([VERSION]))]

    def principal(self):
        token = get_access_token()
        if not token or 'phone.calls' not in token.scopes:
            raise MCPError(code=-32001, message='Owner authorization is required')
        return json.dumps([token.client_id, token.subject], separators=(',', ':')), token.client_id

    def identity(self, params, owner):
        if params.name not in {'transcript.delta', 'call.status'}:
            raise MCPError(code=-32602, message='Unknown event name')
        call_id = str(params.arguments.call_id)
        try:
            self.manager.db.get(call_id)
        except KeyError:
            raise MCPError(code=-32602, message='Call was not found') from None
        identity = json.dumps([owner, params.delivery.url, params.name, {'call_id': call_id}],
                              sort_keys=True, separators=(',', ':'))
        return 'sub_' + hashlib.sha256(identity.encode()).hexdigest(), call_id

    async def list_events(self, ctx, params):
        self.principal()
        if params and params.cursor:
            raise MCPError(code=-32602, message='No further event catalog pages')
        return {'events': definitions()}

    async def subscribe(self, ctx, params):
        owner, client_id = self.principal()
        sid, cid = self.identity(params, owner)
        try:
            callback_url(params.delivery.url)
            validate_secret(params.delivery.secret)
        except ValueError:
            raise MCPError(code=-32602, message='Invalid signing secret') from None
        except CallbackError as exc:
            raise MCPError(code=-32015, message='Callback endpoint verification failed',
                           data={'reason': exc.reason}) from None
        # A requested null/infinite TTL is deliberately granted a finite day.
        ttl = DEFAULT_TTL if params.ttlMs is None else min(DEFAULT_TTL, max(60, params.ttlMs / 1000))
        async with self.lock:
            with self.manager.db.connect() as db:
                count = db.execute('SELECT count(*) FROM mcp_event_subscriptions WHERE active=1 AND expires_at>?',
                                   (time.time(),)).fetchone()[0]
            old = self.get(sid)
            if count >= 50 and not old:
                raise MCPError(code=-32000, message='Subscription limit reached')
            await self.verify(owner, sid, params.delivery)
            # Recheck after the outbound challenge; disconnection can happen in flight.
            if not self.provider.connection_active(client_id):
                raise MCPError(code=-32001, message='Connection was revoked')
            current = time.time()
            ongoing = old and old['active'] and old['expires_at'] > current
            cursor = old['cursor'] if ongoing else self.manager.db.event_head()
            previous = old['secret'] if ongoing and old['secret'] != params.delivery.secret else None
            if ongoing and not previous and old['rotation_until'] > current:
                previous = old['previous_secret']
            rotation_until = current + ROTATION_SECONDS if previous else 0
            if previous and old and previous == old['previous_secret']:
                rotation_until = old['rotation_until']
            expiry = current + ttl
            with self.manager.db.connect() as db:
                db.execute('''INSERT INTO mcp_event_subscriptions
                    (id,owner,client_id,name,call_id,url,secret,previous_secret,rotation_until,expires_at,cursor)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                    secret=excluded.secret, previous_secret=excluded.previous_secret,
                    rotation_until=excluded.rotation_until, expires_at=excluded.expires_at,
                    cursor=excluded.cursor, active=1,
                    failures=CASE WHEN mcp_event_subscriptions.active=1 AND mcp_event_subscriptions.expires_at>? THEN failures ELSE 0 END,
                    next_attempt=CASE WHEN mcp_event_subscriptions.active=1 AND mcp_event_subscriptions.expires_at>? THEN next_attempt ELSE 0 END''',
                    (sid, owner, client_id, params.name, cid, params.delivery.url, params.delivery.secret,
                     previous, rotation_until, expiry, cursor, current, current))
            self.manager.events.notify()
        log_event('mcp_event_subscribed', subscription_id=sid, event_name=params.name)
        return {'id': sid, 'refreshBefore': datetime.fromtimestamp(expiry, timezone.utc).isoformat(),
                'cursor': None, 'truncated': False}

    async def verify(self, owner, sid, delivery):
        cache_key = hashlib.sha256(serialize([owner, delivery.url, delivery.secret])).hexdigest()
        current = time.time()
        self.verifications = {k: v for k, v in self.verifications.items() if v > current}
        if self.verifications.get(cache_key, 0) > current:
            return
        challenge = secrets.token_urlsafe(32)
        event_id = 'msg_verification_' + secrets.token_hex(16)
        body = serialize({'type': 'verification', 'challenge': challenge})
        try:
            async with self.network_limit:
                status, response = await self.sender(delivery.url, body,
                    signed_headers(sid, event_id, delivery.secret, body))
            echoed = json.loads(response).get('challenge') if 200 <= status < 300 else None
            if not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                raise CallbackError('challenge_failed')
        except (ValueError, AttributeError):
            raise MCPError(code=-32015, message='Callback endpoint verification failed',
                           data={'reason': 'challenge_failed'}) from None
        except CallbackError as exc:
            raise MCPError(code=-32015, message='Callback endpoint verification failed',
                           data={'reason': exc.reason}) from None
        self.verifications[cache_key] = current + 300

    async def unsubscribe(self, ctx, params):
        owner, _ = self.principal()
        sid, _ = self.identity(params, owner)
        async with self.lock:
            self.disable(sid)
        self.manager.events.notify()
        return {}

    def get(self, sid):
        with self.manager.db.connect() as db:
            row = db.execute('SELECT * FROM mcp_event_subscriptions WHERE id=?', (sid,)).fetchone()
            return dict(row) if row else None

    def disable(self, sid):
        with self.manager.db.connect() as db:
            db.execute('UPDATE mcp_event_subscriptions SET active=0,secret=?,previous_secret=NULL WHERE id=?',
                       ('', sid))

    def list_subscriptions(self, call_id):
        owner, _ = self.principal()
        self.manager.db.get(call_id)
        with self.manager.db.connect() as db:
            records = [dict(row) for row in db.execute('''SELECT id,name,active,expires_at,failures,
                delivered_events,last_delivery_at FROM mcp_event_subscriptions WHERE owner=? AND call_id=?''',
                (owner, call_id))]
        for row in records:
            row['active'] = bool(row['active'] and row['expires_at'] > time.time())
            row['refreshBefore'] = datetime.fromtimestamp(row.pop('expires_at'), timezone.utc).isoformat()
        return {'call_id': call_id, 'subscriptions': records, 'native_mcp_events_supported': True,
            'protocol_version': VERSION, 'event_names': ['transcript.delta', 'call.status'],
            'subscription_required': not any(r['active'] for r in records),
            'setup': 'Use native MCP events/subscribe when the client offers it. Otherwise use '
                'subscribe_call_events with an actual callback HTTPS URL and whsec_ signing secret. '
                'A tool cannot create a client callback URLs. Do not invent one.',
            'delivery_note': 'Webhooks notify a verified receiver asynchronously. They do not automatically '
                'grant the live voice agent access to ChatGPT plugins. Use send_call_update to send '
                'verified context or owner instructions back into the call.'}

    async def unsubscribe_id(self, sid):
        owner, _ = self.principal()
        record = self.get(sid)
        if not record or record['owner'] != owner:
            raise MCPError(code=-32602, message='Subscription was not found for this connection')
        async with self.lock:
            self.disable(sid)
        self.manager.events.notify()
        return {'subscription_id': sid, 'active': False}

    async def start(self):
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        tasks = [t for t in [self.task, *self.workers.values()] if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        async with self.manager.events.subscribe() as ready:
            while True:
                ready.clear()
                with self.manager.db.connect() as db:
                    ids = [r['id'] for r in db.execute('SELECT id FROM mcp_event_subscriptions WHERE active=1')]
                for sid in ids:
                    if sid not in self.workers or self.workers[sid].done():
                        self.workers[sid] = asyncio.create_task(self.worker(sid))
                self.workers = {sid: task for sid, task in self.workers.items() if not task.done()}
                with suppress(TimeoutError):
                    await asyncio.wait_for(ready.wait(), 5)

    async def worker(self, sid):
        async with self.manager.events.subscribe() as ready:
            while True:
                ready.clear()
                subscription = self.get(sid)
                if not subscription or not subscription['active']:
                    return
                if (subscription['expires_at'] <= time.time()
                        or not self.provider.connection_active(subscription['client_id'])):
                    self.disable(sid)
                    log_event('mcp_event_subscription_stopped', subscription_id=sid)
                    return
                if subscription['next_attempt'] > time.time():
                    with suppress(TimeoutError):
                        await asyncio.wait_for(ready.wait(), min(5, subscription['next_attempt'] - time.time()))
                    continue
                events = self.manager.db.events_after(subscription['cursor'], call_id=subscription['call_id'], limit=50)
                if not events:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(ready.wait(), 5)
                    continue
                for event in events:
                    if event['type'] != subscription['name']:
                        self.ack(sid, event['id'])
                        continue
                    await self.deliver(sid, event)
                    break  # Recheck auth, subscription refresh, expiry and retry timer.

    def ack(self, sid, cursor, *, delivered=False):
        with self.manager.db.connect() as db:
            if delivered:
                db.execute('''UPDATE mcp_event_subscriptions SET delivered_events=delivered_events+1,
                    last_delivery_at=? WHERE id=? AND cursor<?''',
                    (datetime.now(timezone.utc).isoformat(), sid, cursor))
            db.execute('UPDATE mcp_event_subscriptions SET cursor=?,failures=0,next_attempt=0 WHERE id=?',
                       (cursor, sid))

    async def deliver(self, sid, event):
        async with self.network_limit:
            subscription = self.get(sid)
            if (not subscription or not subscription['active'] or subscription['expires_at'] <= time.time()
                    or not self.provider.connection_active(subscription['client_id'])):
                return
            eid = f"evt_{event['call_id']}_{event['id']}"
            data = {**event['data'], 'call_id': event['call_id'],
                    'url': self.manager.settings.public_base_url + '/live?call_id=' + event['call_id']}
            if event['type'] == 'call.status':
                data = {k: v for k, v in data.items() if k in ('call_id', 'url', 'status', 'duration_seconds')}
            body = serialize({'eventId': eid, 'name': event['type'], 'timestamp': event['created_at'],
                              'data': data, 'cursor': None})
            previous = subscription['previous_secret'] if subscription['rotation_until'] > time.time() else None
            try:
                headers = signed_headers(sid, eid, subscription['secret'], body, previous_secret=previous)
                status, _ = await self.sender(subscription['url'], body, headers)
            except CallbackError as exc:
                if exc.reason in ('blocked_address', 'invalid_url'):
                    self.disable(sid)
                    log_event('mcp_event_callback_blocked', subscription_id=sid)
                    return
                status = 413 if exc.reason == 'oversized_payload' else 503
            if 200 <= status < 300:
                self.ack(sid, event['id'], delivered=True)
                log_event('mcp_event_delivered', subscription_id=sid, event_id=eid)
            elif status == 410 or (400 <= status < 500 and status not in (408, 413, 429)) or 300 <= status < 400:
                self.disable(sid)
                log_event('mcp_event_callback_stopped', subscription_id=sid, http_status=status)
            elif status == 413 or subscription['failures'] >= 7:
                self.ack(sid, event['id'])
                log_event('mcp_event_delivery_dropped', subscription_id=sid, event_id=eid, http_status=status)
            else:
                failures = subscription['failures'] + 1
                with self.manager.db.connect() as db:
                    db.execute('UPDATE mcp_event_subscriptions SET failures=?,next_attempt=? WHERE id=?',
                               (failures, time.time() + min(60, 2 ** failures), sid))
                log_event('mcp_event_delivery_retry', subscription_id=sid, event_id=eid, http_status=status)
