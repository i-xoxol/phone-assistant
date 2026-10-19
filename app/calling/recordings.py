"""Noticed call recordings, signed callbacks and private, bounded audio retrieval."""
import asyncio
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

import httpx
from twilio.twiml.voice_response import VoiceResponse
from app.calling.context import utcnow
from app.storage.models import TERMINAL

RECORDING_ID=r'RE[0-9a-fA-F]{32}'
MAX_AUDIO_BYTES=8*1024*1024
STOP_TOOL={'type':'function','name':'stop_call_recording','strict':True,
    'description':'Stop audio recording THIS call when a participant asks not to be recorded. '
        'No permission to delete prior audio or restart recording. Wait for verified success.',
    'parameters':{'type':'object','properties':{},'required':[],'additionalProperties':False}}


def notice_text(settings,language):
    if language=='uk':
        return 'Ця розмова записується.'
    return 'This call is being recorded.'


def voice_recording_policy():
    return '''\nCALL RECORDING: The application played a recording notice before this session and requested audio recording.
Do not repeat it to the same person as your greeting. If an outgoing call first reached an automated menu,
briefly inform the human when they answer that the call is recorded, before discussing the objective.
Inform any new human participant who did not hear the opening notice. Actual recording availability is reported separately; do not invent success.
Backend tools also include stop_call_recording. Delegate immediately if any participant asks not to be recorded.
Wait for verified success before saying recording stopped. Never restart recording or promise deletion of earlier audio.
This stops audio recording only. Live conversation still uses speech processing and captions; if the caller declines
that too, end politely. Do not promise a completely unprocessed call. No new action authority is granted.\n'''


class CallRecordings:
    def __init__(self,manager):
        self.m=manager
        with manager.db.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS call_recording_policy (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id),notice_issued_at TEXT NOT NULL,
                stop_requested_at TEXT,stop_confirmed_at TEXT,error_code TEXT);
                CREATE TABLE IF NOT EXISTS call_recordings (
                recording_id TEXT PRIMARY KEY,call_id TEXT NOT NULL REFERENCES calls(call_id),
                status TEXT NOT NULL CHECK(status IN ('in-progress','completed','absent')),
                duration_seconds INTEGER,channels INTEGER,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS call_recordings_call ON call_recordings(call_id);''')

    def snapshot(self,cid):
        self.m.db.get(cid)
        with self.m.db.connect() as db:
            policy=db.execute('SELECT * FROM call_recording_policy WHERE call_id=?',(cid,)).fetchone()
            rows=[dict(r) for r in db.execute('SELECT * FROM call_recordings WHERE call_id=? ORDER BY created_at',(cid,))]
            event=db.execute("SELECT payload FROM call_events WHERE call_id=? AND type='call.recording' ORDER BY id DESC LIMIT 1",(cid,)).fetchone()
        for r in rows:
            r['audio_path']='/calls/'+cid+'/recordings/'+r['recording_id']+'/audio'
            r['monitor_audio_path']='/live/calls/'+cid+'/recordings/'+r['recording_id']+'/audio'
        return {'enabled_for_new_calls':self.m.settings.call_recording_enabled,
            'policy':dict(policy) if policy else None,'recordings':rows,
            'last_event':json.loads(event['payload']) if event else None,
            'note':'Audio remains private at Twilio. Requested is not proof a recording exists. Audio is available after completion.'}

    def initial_twiml(self,cid,xml):
        if not self.m.settings.call_recording_enabled: return xml
        record=self.m.db.get(cid)
        if record['status'] in TERMINAL: return xml
        # A valid cached notice is required before enabling recording. Never
        # silently start recording if notice preparation was missed.
        language='en'
        if record['direction']=='inbound':
            number='' if self.m.routing.get(record)['caller_identity_ambiguous'] else record['phone_number']
            language='uk' if self.m.knowledge.language_for(number,cid)['language'].split('-')[0]=='uk' else 'en'
        path=self.m.settings.recording_notice_directory/(language+'.wav')
        if not path.is_file():
            # Keep the phone service usable, but never record silently.
            self.m.db.emit(cid,'call.recording',{'state':'unavailable','error_code':'notice_not_prepared'})
            return xml
        with self.m.db.connect() as db:
            claimed=db.execute('INSERT OR IGNORE INTO call_recording_policy(call_id,notice_issued_at) VALUES (?,?)',
                (cid,utcnow().isoformat())).rowcount
        if not claimed: return xml  # No repeated recording or notice on redirects/retries.
        response=VoiceResponse()
        response.play(self.m.settings.public_url('/twilio/recording-notice/'+language+'.wav'))
        response.start().recording(name='call-'+cid,channels='dual',track='both',trim='do-not-trim',
            recording_status_callback=self.m.settings.public_url('/twilio/recordings/'+cid),
            recording_status_callback_method='POST',recording_status_callback_event='in-progress completed absent')
        root=ET.fromstring(str(response));root.extend(list(ET.fromstring(xml)))
        self.m.db.emit(cid,'call.recording',{'state':'requested'})
        return ET.tostring(root,encoding='unicode')

    def callback(self,cid,form):
        record=self.m.db.get(cid)
        if form.get('AccountSid')!=self.m.settings.twilio_account_sid or form.get('CallSid')!=record['twilio_call_sid']:
            raise ValueError('Recording call binding mismatch')
        rid=str(form.get('RecordingSid',''));state=form.get('RecordingStatus')
        if not re.fullmatch(RECORDING_ID,rid) or state not in ('in-progress','completed','absent'):
            raise ValueError('Invalid recording callback')
        duration=int(form['RecordingDuration']) if form.get('RecordingDuration') else None
        channels=int(form['RecordingChannels']) if form.get('RecordingChannels') else None
        if duration is not None and not 0<=duration<=86400: raise ValueError('Invalid recording duration')
        if channels is not None and channels not in (1,2): raise ValueError('Invalid recording channels')
        stamp=utcnow().isoformat()
        with self.m.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM call_recording_policy WHERE call_id=?',(cid,)).fetchone():
                raise ValueError('Recording was not requested for this call')
            old=db.execute('SELECT call_id,status FROM call_recordings WHERE recording_id=?',(rid,)).fetchone()
            if old and old['call_id']!=cid: raise ValueError('Recording binding mismatch')
            if old and (old['status']=='completed' or (old['status']=='absent' and state=='in-progress')): return
            db.execute('''INSERT INTO call_recordings VALUES (?,?,?,?,?,?,?) ON CONFLICT(recording_id)
                DO UPDATE SET status=excluded.status,duration_seconds=COALESCE(excluded.duration_seconds,call_recordings.duration_seconds),
                channels=COALESCE(excluded.channels,call_recordings.channels),updated_at=excluded.updated_at''',
                (rid,cid,state,duration,channels,stamp,stamp))
        self.m.db.emit(cid,'call.recording',{'recording_id':rid,'state':state,'duration_seconds':duration})

    async def stop(self,cid):
        record=self.m.db.get(cid)
        with self.m.db.connect() as db:
            policy=db.execute('SELECT * FROM call_recording_policy WHERE call_id=?',(cid,)).fetchone()
            if not policy: return {'status':'not_recording'}
            if policy['stop_confirmed_at']: return {'status':'stopped'}
            claimed=db.execute('UPDATE call_recording_policy SET stop_requested_at=? WHERE call_id=? AND stop_requested_at IS NULL',
                (utcnow().isoformat(),cid)).rowcount
        if not claimed: return {'status':'stop_unconfirmed','instruction':'Do not promise recording stopped. End the call if the participant declines recording.'}
        try:
            if record['status'] not in TERMINAL:
                await asyncio.wait_for(self.m.gateway.stop_recording(record['twilio_call_sid']),8)
            with self.m.db.connect() as db:
                db.execute('UPDATE call_recording_policy SET stop_confirmed_at=? WHERE call_id=?',(utcnow().isoformat(),cid))
            self.m.db.emit(cid,'call.recording',{'state':'stopped'})
            return {'status':'stopped','instruction':'Audio recording has stopped. Existing transcript processing remains on; if they decline transcription too, end the call.'}
        except Exception:
            with self.m.db.connect() as db:
                db.execute("UPDATE call_recording_policy SET error_code='stop_unconfirmed' WHERE call_id=?",(cid,))
            # A refusal must not leave a conversation recording in the background.
            await self.m.hangup_call(cid)
            return {'status':'call_ended','instruction':'Could not confirm recording stopped; the call was ended.'}

    async def audio(self,cid,rid):
        self.m.db.get(cid)
        if not re.fullmatch(RECORDING_ID,rid): raise KeyError(rid)
        with self.m.db.connect() as db:
            row=db.execute('SELECT status FROM call_recordings WHERE call_id=? AND recording_id=?',(cid,rid)).fetchone()
        if not row or row['status']!='completed': raise KeyError(rid)
        return await self.m.gateway.recording_audio(rid)


async def fetch_recording_audio(settings,rid):
    if not re.fullmatch(RECORDING_ID,rid): raise ValueError('Invalid recording ID')
    url='https://api.twilio.com/2010-04-01/Accounts/'+settings.twilio_account_sid+'/Recordings/'+rid+'.mp3'
    async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
        for hop in range(4):
            parsed=urlsplit(url);host=parsed.hostname or ''
            if parsed.scheme!='https' or parsed.port not in (None,443) or parsed.username or parsed.password or not (
                host=='api.twilio.com' or host.endswith('.twilio.com') or host.endswith('.twiliocdn.com') or host.endswith('.amazonaws.com')):
                raise ValueError('Unapproved media redirect')
            # Provider credentials never follow a redirect to a media host.
            auth=(settings.twilio_account_sid,settings.twilio_auth_token.get_secret_value()) if hop==0 else None
            async with client.stream('GET',url,auth=auth) as response:
                if response.status_code in (301,302,303,307,308):
                    from urllib.parse import urljoin
                    url=urljoin(url,response.headers.get('location',''));continue
                response.raise_for_status();audio=bytearray()
                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio)>MAX_AUDIO_BYTES: raise ValueError('Recording exceeds audio retrieval limit')
                if not audio: raise ValueError('Recording media is empty')
                return bytes(audio)
    raise ValueError('Too many media redirects')
