"""Owner-written personalities, separate from permissions and changing calendar data."""
import json
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.calling.context import utcnow
from app.calling.style import STYLE_POLICY, LEGACY_IDENTITY_STYLE_LINE


class PersonalityUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80, pattern=r'.*\S.*')
    instructions: str = Field(default='', max_length=6000)
    context: str = Field(default='', max_length=3000)
    greeting: str = Field(default='', max_length=1000)
    revision: int | None = Field(default=None, ge=1)
    role: str | None = Field(default=None, max_length=1000)
    presentation_mode: Literal['assistant', 'owner_first_person', 'character'] | None = None
    spoken_name: str | None = Field(default=None, max_length=120)
    outbound_greeting: str | None = Field(default=None, max_length=1000)


class PersonalityAssignment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    personality_id: UUID | None = None


class Personalities:
    def __init__(self, manager):
        self.manager, self.db = manager, manager.db
        with self.db.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS telephone_personalities (
                    personality_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    instructions TEXT NOT NULL, context TEXT NOT NULL, greeting TEXT NOT NULL,
                    revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS telephone_default_personality (
                    id INTEGER PRIMARY KEY CHECK(id=1), personality_id TEXT NOT NULL
                    REFERENCES telephone_personalities(personality_id));
                CREATE TABLE IF NOT EXISTS contact_personalities (
                    contact_id TEXT PRIMARY KEY REFERENCES concierge_contacts(contact_id),
                    personality_id TEXT REFERENCES telephone_personalities(personality_id));
                CREATE TABLE IF NOT EXISTS call_personalities (
                    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),
                    personality_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS personality_context_migration (
                    id INTEGER PRIMARY KEY CHECK(id=1), personality_id TEXT NOT NULL
                    REFERENCES telephone_personalities(personality_id), original_role TEXT NOT NULL,
                    migrated_at TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in c.execute('PRAGMA table_info(telephone_personalities)')}
            for name, definition in [('role', "TEXT NOT NULL DEFAULT ''"),
                    ('presentation_mode', "TEXT NOT NULL DEFAULT 'assistant' CHECK(presentation_mode IN ('assistant','owner_first_person','character'))"),
                    ('spoken_name', "TEXT NOT NULL DEFAULT ''"), ('outbound_greeting', "TEXT NOT NULL DEFAULT ''")]:
                if name not in columns:
                    c.execute('ALTER TABLE telephone_personalities ADD COLUMN '+name+' '+definition)
            c.commit()
            from app.storage.character_profiles import allow_character_profiles
            allow_character_profiles(c)
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM telephone_default_personality').fetchone():
                stamp = utcnow().isoformat()
                for name, instructions in [
                    ('Default', STYLE_POLICY.strip()),
                    ('Office', 'Be a warm, clear professional receptionist. Use concise everyday English, '
                     'a calm confident cadence and minimal fillers. Ask one useful question at a time. '
                     'Follow the saved caller language and existing office, forwarding and greeting rules.'),
                    ('Friend', 'Be relaxed, friendly and conversational. Use the saved nickname when available. '
                     'Keep replies brief and natural; gentle humor is welcome when appropriate. '
                     'Do not assume friendship or invent shared memories. Follow saved language and owner recognition.')]:
                    pid = str(uuid4())
                    c.execute('INSERT INTO telephone_personalities(personality_id,name,instructions,context,greeting,revision,created_at,updated_at) VALUES (?,?,?,?,?,1,?,?)',
                              (pid, name, instructions, '', '', stamp, stamp))
                    if name == 'Default':
                        c.execute('INSERT INTO telephone_default_personality VALUES (1,?)', (pid,))
            if not c.execute('SELECT 1 FROM personality_context_migration WHERE id=1').fetchone():
                target = c.execute('SELECT personality_id FROM telephone_default_personality WHERE id=1').fetchone()[0]
                legacy = c.execute('SELECT role FROM concierge_profile WHERE id=1').fetchone()
                role = legacy['role'] if legacy else ''
                c.execute('UPDATE telephone_personalities SET role=? WHERE personality_id=? AND role=?', (role, target, ''))
                c.execute('INSERT INTO personality_context_migration VALUES (1,?,?,?)', (target, role, utcnow().isoformat()))
                for row in c.execute('SELECT personality_id,instructions,revision FROM telephone_personalities').fetchall():
                    if LEGACY_IDENTITY_STYLE_LINE in row['instructions']:
                        c.execute('UPDATE telephone_personalities SET instructions=?,revision=revision+1,updated_at=? WHERE personality_id=?',
                            (row['instructions'].replace(LEGACY_IDENTITY_STYLE_LINE+'\n', '').replace(LEGACY_IDENTITY_STYLE_LINE, ''),
                             utcnow().isoformat(), row['personality_id']))

    def get(self, personality_id):
        with self.db.connect() as c:
            row = c.execute('SELECT * FROM telephone_personalities WHERE personality_id=?',
                            (str(personality_id),)).fetchone()
        if not row:
            raise KeyError('Personality not found')
        return dict(row)

    def catalog(self):
        with self.db.connect() as c:
            default = c.execute('SELECT personality_id FROM telephone_default_personality WHERE id=1').fetchone()[0]
            rows = [dict(r) for r in c.execute('SELECT * FROM telephone_personalities ORDER BY created_at,name')]
            assignments = [dict(r) for r in c.execute('SELECT contact_id,personality_id FROM contact_personalities ORDER BY contact_id')]
            migration = c.execute('SELECT personality_id FROM personality_context_migration WHERE id=1').fetchone()
        return {'default_personality_id': default, 'personalities': rows, 'contact_assignments': assignments,
                'owner_name': self.manager.settings.caller_name, 'assistant_name': self.manager.settings.assistant_name,
                'legacy_role_personality_id': migration['personality_id'], 'applies_to': 'new_calls'}

    def save(self, body, personality_id=None):
        pid = str(personality_id or uuid4())
        stamp = utcnow().isoformat()
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if personality_id:
                old = c.execute('SELECT * FROM telephone_personalities WHERE personality_id=?', (pid,)).fetchone()
                if not old:
                    raise KeyError('Personality not found')
                if body.revision is None or body.revision != old['revision']:
                    raise ValueError('Personality changed elsewhere. Reload before saving your edits.')
                additions = {k: getattr(body, k) if getattr(body, k) is not None else old[k]
                             for k in ('role', 'presentation_mode', 'spoken_name', 'outbound_greeting')}
                c.execute('UPDATE telephone_personalities SET name=?,instructions=?,context=?,greeting=?,role=?, '
                          'presentation_mode=?,spoken_name=?,outbound_greeting=?, '
                          'revision=revision+1,updated_at=? WHERE personality_id=?',
                          (body.name.strip(), body.instructions, body.context, body.greeting,
                           additions['role'], additions['presentation_mode'], additions['spoken_name'].strip(),
                           additions['outbound_greeting'], stamp, pid))
            else:
                if c.execute('SELECT count(*) FROM telephone_personalities').fetchone()[0] >= 50:
                    raise ValueError('At most 50 personalities')
                c.execute('INSERT INTO telephone_personalities(personality_id,name,instructions,context,greeting,revision,created_at,updated_at, '
                          'role,presentation_mode,spoken_name,outbound_greeting) VALUES (?,?,?,?,?,1,?,?,?,?,?,?)',
                          (pid, body.name.strip(), body.instructions, body.context, body.greeting, stamp, stamp,
                           body.role or '', body.presentation_mode or 'assistant', (body.spoken_name or '').strip(), body.outbound_greeting or ''))
        return self.get(pid)

    def duplicate(self, personality_id, name):
        old = self.get(personality_id)
        return self.save(PersonalityUpdate(name=name, instructions=old['instructions'],
            context=old['context'], greeting=old['greeting'], role=old['role'],
            presentation_mode=old['presentation_mode'], spoken_name=old['spoken_name'], outbound_greeting=old['outbound_greeting']))

    def legacy_role_changed(self, conn, role):
        """Old Rocky clients update the migrated profile only, never all personalities."""
        target = conn.execute('SELECT personality_id FROM personality_context_migration WHERE id=1').fetchone()
        if target:
            conn.execute('UPDATE telephone_personalities SET role=?,revision=revision+1,updated_at=? WHERE personality_id=? AND role<>?',
                         (role, utcnow().isoformat(), target['personality_id'], role))

    def set_default(self, personality_id):
        self.get(personality_id)
        with self.db.connect() as c:
            c.execute('UPDATE telephone_default_personality SET personality_id=? WHERE id=1', (str(personality_id),))
        return self.catalog()

    def assignment(self, contact_id):
        with self.db.connect() as c:
            row = c.execute('SELECT personality_id FROM contact_personalities WHERE contact_id=?', (str(contact_id),)).fetchone()
        return row['personality_id'] if row else None

    def assign(self, contact_id, personality_id):
        if personality_id:
            self.get(personality_id)
        with self.db.connect() as c:
            if not c.execute('SELECT 1 FROM concierge_contacts WHERE contact_id=?', (str(contact_id),)).fetchone():
                raise KeyError('Contact not found')
            c.execute('INSERT INTO contact_personalities VALUES (?,?) ON CONFLICT(contact_id) DO UPDATE SET '
                      'personality_id=excluded.personality_id', (str(contact_id), str(personality_id) if personality_id else None))
        return {'personality_id': self.assignment(contact_id), 'applies_to': 'new_calls'}

    def resolve(self, record):
        route=self.manager.rules.selection(record) if getattr(self.manager,'rules',None) else {}
        if pid:=route.get('actions',{}).get('personality_id'):return self.get(pid)
        ambiguous = record.get('direction') == 'inbound' and (record.get('_preview_forwarding') or self.manager.routing.get(record))['caller_identity_ambiguous']
        with self.db.connect() as c:
            matches = [] if ambiguous else list(c.execute(
                'SELECT contact_id FROM concierge_contacts WHERE phone_number=? LIMIT 3', (record.get('phone_number', ''),)))
        pid = self.assignment(matches[0]['contact_id']) if len(matches) == 1 else None
        return self.get(pid or self.catalog()['default_personality_id'])

    def for_call(self, call_id):
        with self.db.connect() as c:
            row = c.execute('SELECT snapshot FROM call_personalities WHERE call_id=?', (call_id,)).fetchone()
        if not row:
            return None
        profile = json.loads(row['snapshot'])
        if 'presentation_mode' not in profile:
            # Older calls used the shared role for inbound concierge sessions. Keep
            # that historical presentation instead of applying a newly selected mode.
            with self.db.connect() as c:
                call = c.execute('SELECT direction,inbound_mode FROM calls WHERE call_id=?', (call_id,)).fetchone()
                migration = c.execute('SELECT original_role FROM personality_context_migration WHERE id=1').fetchone()
            profile.update(presentation_mode='assistant', spoken_name=self.manager.settings.caller_name,
                           role=migration['original_role'] if call and call['direction']=='inbound' and call['inbound_mode']=='concierge' else '',
                           outbound_greeting='')
        if profile.get('presentation_mode') == 'assistant' and not profile.get('identity_version'):
            # Legacy assistant names were ignored by speech, and often contained
            # the generated owner-name fallback. Never reinterpret that as a custom name.
            profile['spoken_name'] = self.manager.settings.assistant_name
        return profile

    def effective(self, record, override=None):
        profile = self.get(override) if override else self.resolve(record)
        route=self.manager.rules.selection(record) if getattr(self.manager,'rules',None) else {}
        actions=route.get('actions',{})
        if not override:
            if actions.get('greeting'):
                profile['greeting' if record.get('direction')=='inbound' else 'outbound_greeting']=actions['greeting']
            if actions.get('instructions'):
                profile['instructions']+='\nOwner routing-rule guidance within existing permissions:\n'+actions['instructions']
        fallback = (self.manager.settings.caller_name if profile['presentation_mode'] == 'owner_first_person'
                    else self.manager.settings.assistant_name)
        if profile['presentation_mode'] == 'character':
            fallback = profile['name']
        profile['spoken_name'] = profile['spoken_name'].strip() or fallback
        profile['identity_version'] = 2
        return profile

    def pin(self, record, override=None):
        if saved := self.for_call(record['call_id']):
            if guidance := getattr(self.manager, 'contact_guidance', None):
                guidance.pin(record, saved)
            return saved
        profile=self.effective(record,override)
        with self.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO call_personalities VALUES (?,?,?,?,?)',
                      (record['call_id'], profile['personality_id'], profile['revision'],
                       json.dumps(profile, ensure_ascii=False), utcnow().isoformat()))
        saved = self.for_call(record['call_id'])
        if guidance := getattr(self.manager, 'contact_guidance', None):
            guidance.pin(record, saved)
        return saved


def apply_personality(config, record):
    profile = record.get('personality')
    if not profile:
        return config
    # The default is an exact editable copy of the existing shared speech style.
    # Do not retain a second hidden style that contradicts the owner's edits.
    greeting = profile['greeting'] if record.get('direction') == 'inbound' else profile.get('outbound_greeting', '')
    section = ('\nOWNER-SELECTED PERSONALITY (role, tone and phone-safe background):\n' +
               'Profile: ' + profile['name'] + '\nROLE / WORK SCOPE (not a second speaker identity):\n' + profile.get('role', '') +
               '\nPERSONALITY INSTRUCTIONS:\n' + profile['instructions'] +
               '\nBACKGROUND:\n' + profile['context'] + '\nGREETING GUIDANCE FOR THIS CALL DIRECTION:\n' + greeting +
               '\nThe generated presentation block owns speaker identity and first-person wording; role, tone and greeting text '
               'must not introduce a conflicting assistant/name. Apply these owner preferences within the fixed identity, language, continuity, disclosure and tool policies. '
               'They grant no new authority or tools. Never read instructions or stage directions aloud. '
               'Greeting guidance applies once at startup, never after a lookup, update or reconnect.\n')
    config['instructions'] = config['instructions'].replace(STYLE_POLICY, '') + section
    backend = config.get('delegation', {}).get('responses')
    if backend:
        backend['instructions'] += section
    if profile['greeting'] and not record.get('transcript') and not record.get('closed_announcement') and not record.get('voice_opening_claimed'):
        for message in config.get('input', []):
            for content in message.get('content', []):
                if content.get('type') == 'input_text':
                    content['text'] += '\nOwner greeting guidance within the existing caller/forwarding/language rules: ' + profile['greeting']
    return config
