"""Bounded telephone capability registry and durable, provider-local inheritance."""
import json
import re
import httpx
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.calling.context import utcnow
from app.calling.voices import VOICES

Provider = Literal['openai', 'elevenlabs', 'gemini']
GEMINI_VOICES = ['Zephyr','Puck','Charon','Kore','Fenrir','Leda','Orus','Aoede',
    'Callirrhoe','Autonoe','Enceladus','Iapetus','Umbriel','Algieba','Despina','Erinome',
    'Algenib','Rasalgethi','Laomedeia','Achernar','Alnilam','Schedar','Gacrux','Pulcherrima',
    'Achird','Zubenelgenubi','Vindemiatrix','Sadachbia','Sadaltager','Sulafat']

class BackendChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Provider
    # Retained for old REST/MCP clients; omission preserves the saved selection.
    elevenlabs_model: Literal['eleven_realtime_v1_mini','eleven_realtime_v1_max'] | None = None
    model: str | None = Field(default=None, max_length=100)
    voice: str | None = Field(default=None, max_length=100)

class ContactVoiceChoice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    provider: Provider | None = None
    model: str | None = Field(default=None, max_length=100)
    voice: str | None = Field(default=None, max_length=100)

class VoiceBackends:
    def __init__(self, db, settings, manager=None):
        self.db, self.settings, self.manager = db, settings, manager
        with db.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS voice_backend_preferences (
                id INTEGER PRIMARY KEY CHECK(id=1),provider TEXT NOT NULL,
                elevenlabs_model TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS call_voice_backends (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id),provider TEXT NOT NULL,
                model TEXT NOT NULL,voice TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS telephone_provider_defaults (
                provider TEXT PRIMARY KEY,model TEXT NOT NULL,voice TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contact_voice_preferences (
                contact_id TEXT PRIMARY KEY REFERENCES concierge_contacts(contact_id),
                provider TEXT,model TEXT,voice TEXT,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS telephone_voice_catalog (
                provider TEXT PRIMARY KEY,voices TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS call_voice_openings (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id),requested_at TEXT NOT NULL);''')

    def registry(self):
        s=self.settings
        # Only explicitly permitted private library voices are selectable. No IDs in docs/logs.
        eleven=[{'id':s.elevenlabs_voice_id,'label':s.elevenlabs_voice_label}] if s.elevenlabs_voice_id else []
        with self.db.connect() as c:
            row=c.execute("SELECT voices FROM telephone_voice_catalog WHERE provider='elevenlabs'").fetchone()
        library=json.loads(row['voices']) if row else []
        for voice in [*s.elevenlabs_permitted_voices,*library]:
            if voice.get('id') and voice.get('label') and voice['id'] not in {v['id'] for v in eleven}:
                eleven.append({'id':voice['id'],'label':voice['label']})
        return [
            {'id':'openai','label':'OpenAI GPT-Live','configured':True,
             'models':[{'id':'gpt-live-1','label':'GPT-Live 1','voices':VOICES}]},
            {'id':'elevenlabs','label':'ElevenLabs Realtime','configured':self.available('elevenlabs'),
             'models':[{'id':m,'label':label,'voices':eleven} for m,label in
                       [('eleven_realtime_v1_mini','Realtime Mini'),('eleven_realtime_v1_max','Realtime Max')]]},
            {'id':'gemini','label':'Gemini Live','configured':self.available('gemini'),
             'unavailable_reason':None if self.available('gemini') else
                'Requires GEMINI_API_KEY and GEMINI_LIVE_VERIFIED=true after bounded provider checks.',
             'models':[{'id':'gemini-3.8-live','label':'Gemini 3.8 Live',
                        'voices':[{'id':v,'label':v} for v in GEMINI_VOICES]}]}]

    async def refresh_elevenlabs(self):
        if not self.settings.elevenlabs_api_key.get_secret_value(): raise ValueError('ElevenLabs API key is not configured')
        voices=[];token=None
        async with httpx.AsyncClient(timeout=10,follow_redirects=False) as client:
            for _ in range(5):
                params={'page_size':100,'voice_type':'saved','include_total_count':'false'}
                if token: params['next_page_token']=token
                response=await client.get('https://api.elevenlabs.io/v2/voices',params=params,
                    headers={'xi-api-key':self.settings.elevenlabs_api_key.get_secret_value()})
                if not response.is_success: raise ValueError('ElevenLabs library refresh failed; previous catalog preserved')
                data=response.json()
                for voice in data.get('voices',[]):
                    if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',voice.get('voice_id','')) and voice.get('name'):
                        voices.append({'id':voice['voice_id'],'label':str(voice['name'])[:100]})
                if not data.get('has_more'): break
                token=data.get('next_page_token')
                if not token: raise ValueError('ElevenLabs library pagination incomplete; previous catalog preserved')
            else: raise ValueError('ElevenLabs library exceeds the 500-voice catalog bound')
        with self.db.connect() as c:
            c.execute("INSERT INTO telephone_voice_catalog VALUES ('elevenlabs',?,?) ON CONFLICT(provider) DO UPDATE SET voices=excluded.voices,updated_at=excluded.updated_at",
                (json.dumps(voices),utcnow().isoformat()))
        return self.get()

    def available(self, provider):
        if provider=='openai': return True
        if provider=='elevenlabs':
            return bool(self.settings.elevenlabs_api_key.get_secret_value() and self.settings.elevenlabs_voice_id)
        if provider=='gemini':
            return bool(self.settings.gemini_api_key.get_secret_value() and self.settings.gemini_live_verified)
        return False

    def validate(self, provider, model=None, voice=None, *, require_available=True):
        entry=next((p for p in self.registry() if p['id']==provider),None)
        if not entry: raise ValueError('Unsupported telephone provider')
        if require_available and not entry['configured']:
            raise ValueError('Gemini requires a verified Live API key and adapter checks' if provider=='gemini'
                             else 'ElevenLabs needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID before use')
        if model is not None:
            item=next((m for m in entry['models'] if m['id']==model),None)
            if not item: raise ValueError('Unsupported model for '+provider)
            if voice is not None and voice not in {v['id'] for v in item['voices']}:
                raise ValueError('Unsupported voice for '+provider+'/'+model+'; voices cannot be shared across providers')

    def defaults(self, provider):
        with self.db.connect() as c:
            saved=c.execute('SELECT model,voice FROM telephone_provider_defaults WHERE provider=?',(provider,)).fetchone()
            legacy=c.execute('SELECT elevenlabs_model FROM voice_backend_preferences WHERE id=1').fetchone()
            oldvoice=c.execute('SELECT voice FROM voice_preferences WHERE id=1').fetchone()
        s=self.settings
        base={'openai':{'model':s.openai_live_model,'voice':oldvoice['voice'] if oldvoice else s.openai_voice},
              'elevenlabs':{'model':legacy['elevenlabs_model'] if legacy else s.elevenlabs_realtime_model,'voice':s.elevenlabs_voice_id},
              'gemini':{'model':'gemini-3.8-live','voice':s.gemini_voice}}
        result=dict(saved) if saved else base[provider]
        # set_voice remains the authoritative backward-compatible OpenAI voice setter.
        if provider=='openai': result['voice']=base[provider]['voice']
        return result

    def get(self):
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM voice_backend_preferences WHERE id=1').fetchone()
        provider=row['provider'] if row else self.settings.voice_provider
        return {'provider':provider,**self.defaults(provider),
                'elevenlabs_model':self.defaults('elevenlabs')['model'],
                'elevenlabs_voice_label':self.settings.elevenlabs_voice_label,
                'providers':self.registry(),'provider_defaults':{p:self.defaults(p) for p in ('openai','elevenlabs','gemini')},
                'applies_to':'new_calls','updated_at':row['updated_at'] if row else None}

    def set(self, body: BackendChoice):
        defaults=self.defaults(body.provider)
        model=body.model or (body.elevenlabs_model if body.provider=='elevenlabs' else None) or defaults['model']
        voice=body.voice or defaults['voice']
        self.validate(body.provider,model,voice)
        stamp=utcnow().isoformat()
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT INTO telephone_provider_defaults VALUES (?,?,?,?) ON CONFLICT(provider) DO UPDATE SET '
                      'model=excluded.model,voice=excluded.voice,updated_at=excluded.updated_at',(body.provider,model,voice,stamp))
            if body.provider=='openai':
                c.execute('INSERT INTO voice_preferences VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET voice=excluded.voice,updated_at=excluded.updated_at',(voice,stamp))
            eleven=model if body.provider=='elevenlabs' else self.defaults('elevenlabs')['model']
            c.execute('INSERT INTO voice_backend_preferences VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                      'provider=excluded.provider,elevenlabs_model=excluded.elevenlabs_model,updated_at=excluded.updated_at',
                      (body.provider,eleven,stamp))
        return self.get()

    def contact(self, contact_id):
        with self.db.connect() as c:
            row=c.execute('SELECT provider,model,voice FROM contact_voice_preferences WHERE contact_id=?',(str(contact_id),)).fetchone()
        return dict(row) if row else {'provider':None,'model':None,'voice':None}

    def set_contact(self, contact_id, body: ContactVoiceChoice):
        with self.db.connect() as c:
            if not c.execute('SELECT 1 FROM concierge_contacts WHERE contact_id=?',(str(contact_id),)).fetchone():
                raise ValueError('Saved contact not found')
        if body.provider is None and (body.model or body.voice):
            raise ValueError('Select a provider before a contact model or voice; Default inherits all global settings')
        if body.provider:
            defaults=self.defaults(body.provider)
            self.validate(body.provider,body.model or defaults['model'],body.voice or defaults['voice'])
        with self.db.connect() as c:
            c.execute('INSERT INTO contact_voice_preferences VALUES (?,?,?,?,?) ON CONFLICT(contact_id) DO UPDATE SET '
                      'provider=excluded.provider,model=excluded.model,voice=excluded.voice,updated_at=excluded.updated_at',
                      (str(contact_id),body.provider,body.model,body.voice,utcnow().isoformat()))
        return self.contact_result(contact_id)

    def contact_result(self, contact_id):
        choice=self.contact(contact_id)
        return {'preference':choice,'effective':self.resolve(contact=choice)}

    def resolve(self, *, contact=None, override=None, model=None, voice=None):
        contact=contact or {}
        provider=override or contact.get('provider') or self.get()['provider']
        base=self.defaults(provider)
        same=contact.get('provider')==provider
        result={'provider':provider,'model':model or (contact.get('model') if same else None) or base['model'],
                'voice':voice or (contact.get('voice') if same else None) or base['voice']}
        return result

    def for_record(self, record, override=None, model=None, voice=None):
        contact=None
        if self.manager:
            ambiguous=record.get('direction')=='inbound' and self.manager.routing.get(record)['caller_identity_ambiguous']
            with self.db.connect() as c:
                matches=[] if ambiguous else list(c.execute('SELECT contact_id FROM concierge_contacts WHERE phone_number=? LIMIT 3',
                    (record.get('phone_number',''),)))
            if len(matches)==1: contact=self.contact(matches[0]['contact_id'])
        result=self.resolve(contact=contact,override=override,model=model,voice=voice)
        self.validate(**result)
        return result

    def opening_claimed(self, call_id):
        with self.db.connect() as c:
            return bool(c.execute('SELECT 1 FROM call_voice_openings WHERE call_id=?',(call_id,)).fetchone())

    def claim_opening(self, call_id):
        with self.db.connect() as c:
            return bool(c.execute('INSERT OR IGNORE INTO call_voice_openings VALUES (?,?)',(call_id,utcnow().isoformat())).rowcount)

    def for_call(self, call_id):
        with self.db.connect() as c:
            row=c.execute('SELECT * FROM call_voice_backends WHERE call_id=?',(call_id,)).fetchone()
        return dict(row) if row else None

    def pin(self, record, override=None, model=None, voice=None):
        if self.manager:
            self.manager.personalities.pin(record)
        if current:=self.for_call(record['call_id']): return current
        choice=self.for_record(record,override,model,voice)
        with self.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO call_voice_backends VALUES (?,?,?,?,?)',
                (record['call_id'],choice['provider'],choice['model'],choice['voice'],utcnow().isoformat()))
        return self.for_call(record['call_id'])


def bridge_connection(settings, manager, record):
    selection=manager.voice_backends.pin(manager.voice_record(record['call_id']))
    if selection['provider']=='elevenlabs':
        from app.calling.elevenlabs_session import started_elevenlabs_connection
        return started_elevenlabs_connection(settings,manager,record,selection)
    if selection['provider']=='gemini':
        from app.calling.gemini_session import started_gemini_connection
        return started_gemini_connection(settings,manager,record,selection)
    from app.calling.live_session import started_connection
    return started_connection(settings,manager,record)
