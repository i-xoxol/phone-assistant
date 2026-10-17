"""Owner-selected Live voices. Changes affect new calls, not an active session."""
from pydantic import BaseModel,ConfigDict,Field,field_validator
from app.calling.context import utcnow

# API names and descriptions from the official GPT-Live conversation guide.
VOICES=[{'id':'marin','label':'Marin'},
    {'id':'gleam','label':'Gleam · North American · feminine'},
    {'id':'meridian','label':'Meridian · North American · masculine'},
    {'id':'quartz','label':'Quartz · Australian · feminine'},
    {'id':'ripple','label':'Ripple · Australian · masculine'},
    {'id':'vesper','label':'Vesper · British · masculine'},
    {'id':'willow','label':'Willow · Irish · feminine'},
    {'id':'stone','label':'Stone · Irish · masculine'},
    {'id':'bossa','label':'Bossa · Brazilian Portuguese · feminine'},
    {'id':'tempo','label':'Tempo · Brazilian Portuguese · masculine'},
    {'id':'beacon','label':'Beacon · Filipino English · masculine'},
    {'id':'delta','label':'Delta · Southern US · feminine'},
    {'id':'cinder','label':'Cinder · Southern US · masculine'},
    {'id':'nuri','label':'Nuri · German · feminine'},
    {'id':'juni','label':'Juni · German · feminine'},
    {'id':'flitz','label':'Flitz · German · masculine'},
    {'id':'brise','label':'Brise · French · feminine'},
    {'id':'sillage','label':'Sillage · French · masculine'},
    {'id':'nira','label':'Nira · Hindi · feminine'},
    {'id':'harema','label':'Harema · Japanese · feminine'},
    {'id':'shida','label':'Shida · Japanese · masculine'},
    {'id':'noeul','label':'Noeul · Korean · feminine'}]


class VoiceChoice(BaseModel):
    model_config=ConfigDict(extra='forbid')
    voice: str=Field(max_length=30)

    @field_validator('voice')
    @classmethod
    def supported(cls,value):
        if value not in {v['id'] for v in VOICES}:
            raise ValueError('Choose a voice from get_voice_settings')
        return value


class VoicePreferences:
    def __init__(self,db,settings):
        self.db,self.settings=db,settings
        with db.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS voice_preferences(id INTEGER PRIMARY KEY CHECK(id=1),voice TEXT NOT NULL,updated_at TEXT NOT NULL)')

    def get(self):
        with self.db.connect() as conn:
            row=conn.execute('SELECT voice,updated_at FROM voice_preferences WHERE id=1').fetchone()
        return {'voice':row['voice'] if row else self.settings.openai_voice,'voices':VOICES,
                'applies_to':'new_calls','updated_at':row['updated_at'] if row else None}

    def set(self,body:VoiceChoice):
        with self.db.connect() as conn:
            conn.execute('INSERT INTO voice_preferences VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET voice=excluded.voice,updated_at=excluded.updated_at',
                         (body.voice,utcnow().isoformat()))
        return self.get()

    def pin(self,record):
        with self.db.connect() as conn:
            conn.execute('UPDATE calls SET voice_name=? WHERE call_id=? AND voice_name IS NULL',
                         (self.get()['voice'],record['call_id']))
            return conn.execute('SELECT voice_name FROM calls WHERE call_id=?',(record['call_id'],)).fetchone()['voice_name']
