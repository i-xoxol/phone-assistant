"""Small backend registry and durable selection. Active calls never switch providers."""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.calling.context import utcnow

Provider = Literal['openai', 'elevenlabs']


class BackendChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Provider
    elevenlabs_model: Literal['eleven_realtime_v1_mini', 'eleven_realtime_v1_max'] = 'eleven_realtime_v1_mini'


class VoiceBackends:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings
        with db.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS voice_backend_preferences (
                    id INTEGER PRIMARY KEY CHECK(id=1), provider TEXT NOT NULL,
                    elevenlabs_model TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS call_voice_backends (
                    call_id TEXT PRIMARY KEY REFERENCES calls(call_id), provider TEXT NOT NULL,
                    model TEXT NOT NULL, voice TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')

    def available(self, provider):
        return provider == 'openai' or bool(self.settings.elevenlabs_api_key.get_secret_value()
                                           and self.settings.elevenlabs_voice_id)

    def get(self):
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM voice_backend_preferences WHERE id=1').fetchone()
        return {'provider': row['provider'] if row else self.settings.voice_provider,
                'elevenlabs_model': row['elevenlabs_model'] if row else self.settings.elevenlabs_realtime_model,
                'elevenlabs_voice_label': self.settings.elevenlabs_voice_label,
                'providers': [{'id': 'openai', 'label': 'OpenAI GPT-Live', 'configured': True},
                              {'id': 'elevenlabs', 'label': 'ElevenLabs Realtime',
                               'configured': self.available('elevenlabs')}],
                'applies_to': 'new_calls', 'updated_at': row['updated_at'] if row else None}

    def validate(self, provider):
        if not self.available(provider):
            raise ValueError('ElevenLabs needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID before use')

    def set(self, body: BackendChoice):
        self.validate(body.provider)
        with self.db.connect() as conn:
            conn.execute('''INSERT INTO voice_backend_preferences VALUES (1,?,?,?)
                ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,
                elevenlabs_model=excluded.elevenlabs_model,updated_at=excluded.updated_at''',
                (body.provider, body.elevenlabs_model, utcnow().isoformat()))
        return self.get()

    def for_call(self, call_id):
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM call_voice_backends WHERE call_id=?', (call_id,)).fetchone()
        return dict(row) if row else None

    def pin(self, record, override=None):
        if current := self.for_call(record['call_id']):
            return current
        preference = self.get()
        provider = override or preference['provider']
        self.validate(provider)
        model = preference['elevenlabs_model'] if provider == 'elevenlabs' else self.settings.openai_live_model
        voice = self.settings.elevenlabs_voice_id if provider == 'elevenlabs' else (
            record.get('voice_name') or self.settings.openai_voice)
        with self.db.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO call_voice_backends VALUES (?,?,?,?,?)',
                         (record['call_id'], provider, model, voice, utcnow().isoformat()))
        return self.for_call(record['call_id'])


def bridge_connection(settings, manager, record):
    """Lazy imports leave the existing OpenAI startup/preconnect path unchanged."""
    selection = manager.voice_backends.pin(manager.voice_record(record['call_id']))
    if selection['provider'] == 'elevenlabs':
        from app.calling.elevenlabs_session import started_elevenlabs_connection
        return started_elevenlabs_connection(settings, manager, record, selection)
    from app.calling.live_session import started_connection
    return started_connection(settings, manager, record)
