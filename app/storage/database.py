import sqlite3
import json
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.storage.models import CallRequest, CallStatus, TERMINAL


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.on_event = lambda: None
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT, phone_number TEXT NOT NULL,
                    recipient TEXT NOT NULL, objective TEXT NOT NULL, context TEXT NOT NULL,
                    max_duration_minutes INTEGER NOT NULL,
                    twilio_call_sid TEXT UNIQUE, openai_session_id TEXT,
                    status TEXT NOT NULL CHECK(status IN ('queued','dialing','ringing',
                        'in_progress','completed','failed','busy','no_answer','cancelled')),
                    duration_seconds INTEGER NOT NULL DEFAULT 0,
                    callback_sequence INTEGER NOT NULL DEFAULT -1,
                    error_message TEXT, voice_finalized INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS constraints (
                    call_id TEXT NOT NULL REFERENCES calls(call_id),
                    position INTEGER NOT NULL, text TEXT NOT NULL,
                    PRIMARY KEY(call_id, position)
                );
                CREATE TABLE IF NOT EXISTS transcript_events (
                    id INTEGER PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(call_id),
                    event_id TEXT NOT NULL, speaker TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
                    UNIQUE(call_id, event_id)
                );
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(calls)')}
            if 'ivr_mode' not in columns:
                db.execute('ALTER TABLE calls ADD COLUMN ivr_mode INTEGER NOT NULL DEFAULT 0')
            db.execute('''CREATE TABLE IF NOT EXISTS keypad_events (
                id INTEGER PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(call_id),
                created_at TEXT NOT NULL, digits TEXT NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL)''')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS call_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES calls(call_id),
                    type TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS call_events_call ON call_events(call_id,id);
                CREATE TABLE IF NOT EXISTS webhook_state (
                    destination TEXT PRIMARY KEY, last_event_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_tool_operations (
                    call_id TEXT NOT NULL REFERENCES calls(call_id), tool_call_id TEXT NOT NULL,
                    name TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(call_id,tool_call_id)
                );
                CREATE TABLE IF NOT EXISTS call_updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, update_id TEXT UNIQUE NOT NULL,
                    call_id TEXT NOT NULL REFERENCES calls(call_id), created_at TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('context','say','instructions')),
                    content TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN
                        ('queued','sent','acknowledged','rejected','unconfirmed')),
                    sent_at TEXT, acknowledged_at TEXT, openai_session_id TEXT,
                    start_ms INTEGER, end_ms INTEGER, error_code TEXT, restored_at TEXT
                );
                CREATE INDEX IF NOT EXISTS call_updates_call ON call_updates(call_id,id);
            ''')

    def _event(self, db, call_id: str, kind: str, data: dict):
        db.execute('INSERT INTO call_events(call_id,type,created_at,payload) VALUES (?,?,?,?)',
                   (call_id, kind, now(), json.dumps(data, ensure_ascii=False)))

    def emit(self, call_id: str, kind: str, data: dict):
        with self.connect() as db:
            self._event(db, call_id, kind, data)
        self.on_event()

    def claim_tool(self, call_id: str, tool_call_id: str, name: str) -> bool:
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM voice_tool_operations WHERE call_id=? AND tool_call_id=?',
                          (call_id, tool_call_id)).fetchone():
                return False
            count = db.execute('SELECT count(*) FROM voice_tool_operations WHERE call_id=?', (call_id,)).fetchone()[0]
            if count >= 20:
                raise ValueError('Voice tool limit reached')
            return bool(db.execute('INSERT OR IGNORE INTO voice_tool_operations VALUES (?,?,?,?,?)',
                (call_id, tool_call_id, name, 'requested', now())).rowcount)

    def finish_tool(self, call_id: str, tool_call_id: str, status: str):
        with self.connect() as db:
            db.execute('UPDATE voice_tool_operations SET status=? WHERE call_id=? AND tool_call_id=?',
                       (status, call_id, tool_call_id))

    def call_update(self, call_id, update_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM call_updates WHERE call_id=? AND update_id=?',
                             (call_id, update_id)).fetchone()
            return dict(row) if row else None

    def queue_update(self, call_id, update_id, content, mode):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM call_updates WHERE update_id=?', (update_id,)).fetchone()
            if old:
                if (old['call_id'], old['content'], old['mode']) != (call_id, content, mode):
                    raise ValueError('Update ID was already used for different content')
                return dict(old)
            call = db.execute('SELECT status FROM calls WHERE call_id=?', (call_id,)).fetchone()
            if not call:
                raise KeyError(call_id)
            if call['status'] in TERMINAL:
                raise ValueError('Call has ended')
            if db.execute('SELECT count(*) FROM call_updates WHERE call_id=?', (call_id,)).fetchone()[0] >= 20:
                raise ValueError('Call update limit reached (20); send concise combined updates')
            db.execute('INSERT INTO call_updates(update_id,call_id,created_at,mode,content,status) VALUES (?,?,?,?,?,?)',
                       (update_id, call_id, now(), mode, content, 'queued'))
            self._event(db, call_id, 'call.context_update', {'update_id': update_id, 'mode': mode, 'status': 'queued'})
        self.on_event()
        return self.call_update(call_id, update_id)

    def claim_update(self, call_id, update_id, session_id):
        with self.connect() as db:
            changed = db.execute('''UPDATE call_updates SET status='sent',sent_at=?,openai_session_id=?
                WHERE call_id=? AND update_id=? AND status='queued' ''',
                (now(), session_id, call_id, update_id)).rowcount
        return bool(changed)

    def finish_update(self, call_id, update_id, session_id, status, *, start_ms=None, end_ms=None, error_code=None):
        if status not in ('acknowledged', 'rejected', 'unconfirmed'):
            raise ValueError('Unsupported update status')
        with self.connect() as db:
            changed = db.execute('''UPDATE call_updates SET status=?,acknowledged_at=?,start_ms=?,end_ms=?,error_code=?
                WHERE call_id=? AND update_id=? AND openai_session_id=? AND status IN ('sent','unconfirmed')''',
                (status, now() if status == 'acknowledged' else None, start_ms, end_ms, error_code,
                 call_id, update_id, session_id)).rowcount
            if changed:
                self._event(db, call_id, 'call.context_update', {'update_id': update_id, 'status': status})
        if changed:
            self.on_event()
        return bool(changed)

    def restore_updates(self, call_id, updates):
        # Called only after session.started, for updates included in its prompt.
        with self.connect() as db:
            db.executemany('UPDATE call_updates SET restored_at=? WHERE call_id=? AND update_id=?',
                          [(now(), call_id, r['update_id']) for r in updates if r['status'] in ('sent', 'acknowledged')])

    def event_head(self) -> int:
        with self.connect() as db:
            return db.execute('SELECT coalesce(max(id),0) FROM call_events').fetchone()[0]

    def events_after(self, cursor: int, *, call_id: str | None = None, limit: int = 500) -> list[dict]:
        with self.connect() as db:
            query = 'SELECT * FROM call_events WHERE id>?'
            args = [cursor]
            if call_id:
                query += ' AND call_id=?'
                args.append(call_id)
            return [{ 'id': r['id'], 'call_id': r['call_id'], 'type': r['type'],
                      'created_at': r['created_at'], 'data': json.loads(r['payload']) }
                    for r in db.execute(query + ' ORDER BY id LIMIT ?', (*args, limit))]

    def recent_calls(self) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute('''SELECT call_id,recipient,phone_number,objective,
                created_at,started_at,status,duration_seconds FROM calls
                ORDER BY created_at DESC LIMIT 50''')]

    def webhook_cursor(self, destination: str) -> int:
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO webhook_state VALUES (?,?)', (destination, self.event_head()))
            return db.execute('SELECT last_event_id FROM webhook_state WHERE destination=?', (destination,)).fetchone()[0]

    def webhook_ack(self, destination: str, cursor: int):
        with self.connect() as db:
            db.execute('UPDATE webhook_state SET last_event_id=? WHERE destination=?', (cursor, destination))

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            yield db

    def create(self, request: CallRequest) -> str:
        call_id = str(uuid4())
        with self.connect() as db:
            db.execute('''INSERT INTO calls (call_id, created_at, phone_number, recipient,
                objective, context, max_duration_minutes, status) VALUES (?,?,?,?,?,?,?,?)''',
                (call_id, now(), request.phone_number, request.recipient, request.objective,
                 request.context, request.max_duration_minutes, 'queued'))
            db.execute('UPDATE calls SET ivr_mode=? WHERE call_id=?', (int(request.ivr_mode), call_id))
            db.executemany('INSERT INTO constraints VALUES (?,?,?)',
                          [(call_id, i, text) for i, text in enumerate(request.constraints)])
            self._event(db, call_id, 'call.created', {'status': 'queued', 'recipient': request.recipient})
        self.on_event()
        return call_id

    def get(self, call_id: str) -> dict:
        with self.connect() as db:
            row = db.execute('SELECT * FROM calls WHERE call_id=?', (call_id,)).fetchone()
            if row is None:
                raise KeyError(call_id)
            result = dict(row)
            result['ivr_mode'] = bool(result['ivr_mode'])
            result['keypad_events'] = [dict(r) for r in db.execute(
                'SELECT created_at,digits,reason,status FROM keypad_events WHERE call_id=? ORDER BY id', (call_id,))]
            result['voice_tool_operations'] = [dict(r) for r in db.execute(
                'SELECT name,status,created_at FROM voice_tool_operations WHERE call_id=? ORDER BY created_at', (call_id,))]
            result['call_updates'] = [dict(r) for r in db.execute(
                'SELECT * FROM call_updates WHERE call_id=? ORDER BY id', (call_id,))]
            result['voice_events'] = [{'created_at':r['created_at'], 'type':r['type'],
                **json.loads(r['payload'])} for r in db.execute(
                "SELECT created_at,type,payload FROM call_events WHERE call_id=? AND type IN ('call.media_stream','call.voice_bridge_error') ORDER BY id", (call_id,))]
            result['constraints'] = [r['text'] for r in db.execute(
                'SELECT text FROM constraints WHERE call_id=? ORDER BY position', (call_id,))]
            result['transcript'] = [
                {'source_event_id': r['event_id'], 'speaker': r['speaker'], 'timestamp': r['start_ms'] / 1000,
                 'end_timestamp': r['end_ms'] / 1000, 'text': r['text']}
                for r in db.execute('''SELECT * FROM transcript_events WHERE call_id=?
                    ORDER BY start_ms, id''', (call_id,))]
            return result

    def keypad(self, call_id: str, digits: str, reason: str, status: str):
        with self.connect() as db:
            db.execute('INSERT INTO keypad_events(call_id,created_at,digits,reason,status) VALUES (?,?,?,?,?)',
                       (call_id, now(), digits, reason, status))
            self._event(db, call_id, 'call.keypad', {'digits': digits, 'reason': reason, 'status': status})
        self.on_event()

    def update(self, call_id: str, **fields):
        allowed = {'twilio_call_sid', 'openai_session_id', 'error_message', 'voice_finalized'}
        if not fields or not fields.keys() <= allowed:
            raise ValueError('Unsupported fields')
        with self.connect() as db:
            db.execute(f"UPDATE calls SET {', '.join(k + '=?' for k in fields)} WHERE call_id=?",
                       (*fields.values(), call_id))
            public = {k: v for k, v in fields.items() if k in ('error_message', 'voice_finalized')}
            if public:
                self._event(db, call_id, 'call.updated', public)
        if public:
            self.on_event()

    def voice_error(self, call_id: str, message: str, details: dict):
        # Callers supply application-owned messages and metadata only. Keep the
        # first error even when disconnect/finalization callbacks arrive later.
        with self.connect() as db:
            db.execute('UPDATE calls SET error_message=coalesce(error_message,?) WHERE call_id=?',
                       (message, call_id))
            self._event(db, call_id, 'call.voice_bridge_error', details)
            row = db.execute('SELECT status,duration_seconds,error_message FROM calls WHERE call_id=?', (call_id,)).fetchone()
            self._event(db, call_id, 'call.status', dict(row))
        self.on_event()

    def transition(self, call_id: str, status: str, *, sequence: int | None = None,
                   duration: int | None = None) -> bool:
        CallStatus(status)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM calls WHERE call_id=?', (call_id,)).fetchone()
            if row is None:
                raise KeyError(call_id)
            # Ignore duplicates and callbacks delivered out of order.
            if sequence is not None and sequence <= row['callback_sequence']:
                return False
            if row['status'] in TERMINAL:
                if duration is not None:
                    db.execute('UPDATE calls SET duration_seconds=? WHERE call_id=?',
                               (max(0, duration), call_id))
                    self._event(db, call_id, 'call.status', {'status': row['status'], 'duration_seconds': max(0, duration)})
                changed = False
            else:
                ranks = {'queued': 0, 'dialing': 1, 'ringing': 2, 'in_progress': 3}
                if status not in TERMINAL and ranks[status] < ranks[row['status']]:
                    return False
                started = row['started_at'] or (now() if status == 'in_progress' else None)
                completed = now() if status in TERMINAL else None
                db.execute('''UPDATE calls SET status=?, started_at=?, completed_at=?,
                    callback_sequence=?, duration_seconds=? WHERE call_id=?''',
                    (status, started, completed,
                     sequence if sequence is not None else row['callback_sequence'],
                     max(0, duration) if duration is not None else row['duration_seconds'], call_id))
                changed = row['status'] != status
                if status in TERMINAL:
                    db.execute("""UPDATE call_updates SET status=CASE WHEN status='queued' THEN 'rejected' ELSE 'unconfirmed' END,
                        error_code='call_ended' WHERE call_id=? AND status IN ('queued','sent')""", (call_id,))
                if changed or duration is not None:
                    self._event(db, call_id, 'call.status', {'status': status, 'started_at': started,
                        'duration_seconds': max(0, duration) if duration is not None else row['duration_seconds']})
        self.on_event()
        return changed

    def transcript(self, call_id: str, event: dict):
        speaker = 'callee' if event['type'] == 'session.input_transcript.delta' else 'assistant'
        with self.connect() as db:
            inserted = db.execute('''INSERT OR IGNORE INTO transcript_events
                (call_id,event_id,speaker,start_ms,end_ms,text) VALUES (?,?,?,?,?,?)''',
                (call_id, event['event_id'], speaker, event['start_ms'], event['end_ms'], event['delta']))
            if inserted.rowcount:
                self._event(db, call_id, 'transcript.delta', {'speaker': speaker,
                    'timestamp': event['start_ms'] / 1000, 'end_timestamp': event['end_ms'] / 1000,
                    'text': event['delta'], 'source_event_id': event['event_id']})
        if inserted.rowcount:
            self.on_event()
