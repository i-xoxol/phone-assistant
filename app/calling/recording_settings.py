"""New-call recording policy and owner-managed immutable notice assets."""
import asyncio
import hashlib
import io
import os
import re
import tempfile
import wave
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field
from app.calling.context import utcnow

MAX_UPLOAD=2*1024*1024
LANGUAGES=('en','uk')
TEXTS={'en':'This call is being recorded.','uk':'Ця розмова записується.'}

class RecordingChoice(BaseModel):
    model_config=ConfigDict(extra='forbid')
    recording_enabled: bool
    announcement_enabled: bool

class NoticeText(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text: str = Field(min_length=1,max_length=180)

class RecordingSettings:
    def __init__(self, manager):
        self.m=manager
        self.generation_lock=asyncio.Lock()
        with manager.db.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS telephone_recording_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),recording_enabled INTEGER NOT NULL,
                announcement_enabled INTEGER NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS call_recording_settings (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id),recording_enabled INTEGER NOT NULL,
                announcement_enabled INTEGER NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recording_notice_assets (
                asset_id TEXT PRIMARY KEY,language TEXT NOT NULL,text TEXT NOT NULL,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS recording_notice_choices (
                language TEXT PRIMARY KEY,active_asset TEXT,draft_asset TEXT);''')

    def get(self):
        with self.m.db.connect() as c:
            row=c.execute('SELECT * FROM telephone_recording_settings WHERE id=1').fetchone()
        return {'recording_enabled':bool(row['recording_enabled']) if row else self.m.settings.call_recording_enabled,
                'announcement_enabled':bool(row['announcement_enabled']) if row else self.m.settings.recording_announcement_enabled,
                'applies_to':'new_calls','transcription':'Live speech processing and captions remain enabled separately.',
                'updated_at':row['updated_at'] if row else None}

    def set(self, body):
        with self.m.db.connect() as c:
            c.execute('INSERT INTO telephone_recording_settings VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                'recording_enabled=excluded.recording_enabled,announcement_enabled=excluded.announcement_enabled,updated_at=excluded.updated_at',
                (int(body.recording_enabled),int(body.announcement_enabled),utcnow().isoformat()))
        return self.get()

    def pin(self, cid):
        body=self.get()
        with self.m.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO call_recording_settings VALUES (?,?,?,?)',
                (cid,int(body['recording_enabled']),int(body['announcement_enabled']),utcnow().isoformat()))
            row=c.execute('SELECT * FROM call_recording_settings WHERE call_id=?',(cid,)).fetchone()
        return {**dict(row),'recording_enabled':bool(row['recording_enabled']),'announcement_enabled':bool(row['announcement_enabled'])}

    def language(self, language):
        if language not in LANGUAGES: raise ValueError('Notice language must be en or uk')

    def asset_path(self, language, asset_id=None):
        self.language(language)
        if asset_id:
            if not re.fullmatch(r'[0-9a-f]{64}',asset_id): raise ValueError('Invalid notice asset')
            with self.m.db.connect() as c:
                if not c.execute('SELECT 1 FROM recording_notice_assets WHERE language=? AND asset_id=?',(language,asset_id)).fetchone():
                    raise ValueError('Notice asset not found')
            return self.m.settings.recording_notice_directory/(asset_id+'.wav')
        with self.m.db.connect() as c:
            row=c.execute('SELECT active_asset FROM recording_notice_choices WHERE language=?',(language,)).fetchone()
        return self.asset_path(language,row['active_asset']) if row and row['active_asset'] else self.m.settings.recording_notice_directory/(language+'.wav')

    def notices(self):
        result=[]
        for language in LANGUAGES:
            with self.m.db.connect() as c:
                choice=c.execute('SELECT * FROM recording_notice_choices WHERE language=?',(language,)).fetchone()
                active=c.execute('SELECT * FROM recording_notice_assets WHERE asset_id=?',(choice['active_asset'],)).fetchone() if choice else None
                draft=c.execute('SELECT * FROM recording_notice_assets WHERE asset_id=?',(choice['draft_asset'],)).fetchone() if choice else None
            result.append({'language':language,'text':active['text'] if active else TEXTS[language],
                'active_asset':dict(active) if active else None,'draft':dict(draft) if draft else None,
                'prepared':self.asset_path(language).is_file()})
        return result

    @staticmethod
    def normalize(audio):
        if not audio or len(audio)>MAX_UPLOAD: raise ValueError('Upload a WAV clip of at most 2 MiB and eight seconds')
        try:
            with wave.open(io.BytesIO(audio),'rb') as wav:
                if wav.getnchannels()!=1 or wav.getsampwidth()!=2 or wav.getcomptype()!='NONE' or not 8000<=wav.getframerate()<=48000:
                    raise ValueError('Use mono, uncompressed 16-bit PCM WAV at 8–48 kHz')
                rate=wav.getframerate(); frames=wav.getnframes()
                if not 0<frames<=rate*8: raise ValueError('Notice must last between zero and eight seconds')
                pcm=wav.readframes(frames)
                if len(pcm)!=frames*2: raise ValueError('Truncated WAV clip')
            import numpy as np
            import soxr
            samples=np.frombuffer(pcm,dtype='<i2')
            if rate!=24000: samples=soxr.resample(samples,rate,24000,quality='HQ')
            output=io.BytesIO()
            with wave.open(output,'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
                wav.writeframes(np.asarray(samples,dtype='<i2').tobytes())
            return output.getvalue()
        except (wave.Error,EOFError) as exc:
            raise ValueError('Invalid PCM WAV clip') from exc

    def stage(self, language, text, audio):
        self.language(language)
        text=NoticeText(text=text).text.strip()
        if not text: raise ValueError('Notice text cannot be blank')
        audio=self.normalize(audio)
        asset_id=hashlib.sha256(language.encode()+b'\0'+text.encode()+b'\0'+audio).hexdigest()
        folder=self.m.settings.recording_notice_directory;folder.mkdir(parents=True,exist_ok=True)
        fd,temporary=tempfile.mkstemp(prefix='.notice-',dir=folder)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(audio);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,folder/(asset_id+'.wav'))
        finally:
            if os.path.exists(temporary): os.unlink(temporary)
        with self.m.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO recording_notice_assets VALUES (?,?,?,?)',(asset_id,language,text,utcnow().isoformat()))
            c.execute('INSERT INTO recording_notice_choices(language,draft_asset) VALUES (?,?) '
                'ON CONFLICT(language) DO UPDATE SET draft_asset=excluded.draft_asset',(language,asset_id))
        return self.notices()

    async def generate(self, language, text):
        self.language(language)
        text=NoticeText(text=text.strip()).text
        if self.generation_lock.locked(): raise ValueError('A notice is already being generated; please wait')
        async with self.generation_lock:
            from scripts.prepare_recording_notices import render_notice
            audio=await render_notice(self.m.settings,text)
            return self.stage(language,text,audio)

    def activate(self, language, asset_id):
        path=self.asset_path(language,asset_id)
        if not path.is_file(): raise ValueError('Prepared notice audio missing; current notice preserved')
        with self.m.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT draft_asset FROM recording_notice_choices WHERE language=?',(language,)).fetchone()
            if not row or row['draft_asset']!=asset_id: raise ValueError('Preview changed; reload and activate the current draft')
            c.execute('UPDATE recording_notice_choices SET active_asset=?,draft_asset=NULL WHERE language=?',(asset_id,language))
        return self.notices()

    def cache_current(self, language):
        """Snapshot a legacy file once; playback URLs keep the same bytes after edits."""
        with self.m.db.connect() as c:
            active=c.execute('SELECT active_asset FROM recording_notice_choices WHERE language=?',(language,)).fetchone()
        if active and active['active_asset']:
            path=self.asset_path(language,active['active_asset'])
            return active['active_asset'] if path.is_file() else None
        path=self.asset_path(language)
        if not path.is_file(): return None
        audio=path.read_bytes()
        asset_id=hashlib.sha256(language.encode()+audio).hexdigest()
        target=path.parent/(asset_id+'.wav')
        if not target.exists():
            fd,temporary=tempfile.mkstemp(prefix='.notice-',dir=path.parent)
            try:
                with os.fdopen(fd,'wb') as stream: stream.write(audio)
                os.replace(temporary,target)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        with self.m.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO recording_notice_assets VALUES (?,?,?,?)',(asset_id,language,TEXTS[language],utcnow().isoformat()))
        return asset_id
