"""Short, cached keypad prompts and a once-per-transfer natural voice briefing."""
import asyncio
import hashlib
import io
import json
import time
import wave

from app.calling.speech import synthesize
from app.logging_config import log_event


PROMPTS={
    'menu':'1 for answer. 2, decline. 3, simple answer.',
    'recorded':'Recorded call.',
    'connected-coaching':'Connected. Transcription and coaching on.',
    'connected-simple':'Connected. Transcription on.',
}


def prompt_key(settings,name):
    value=json.dumps([PROMPTS[name],settings.owner_screen_speech_model,settings.owner_screen_speech_voice])
    return name+'-'+hashlib.sha256(value.encode()).hexdigest()[:16]+'.wav'


def valid_wav(audio,maximum=20):
    try:
        with wave.open(io.BytesIO(audio)) as wav:
            return (wav.getnchannels()==1 and wav.getsampwidth()==2 and
                0<wav.getnframes()/wav.getframerate()<=maximum)
    except (wave.Error,EOFError,ValueError,ZeroDivisionError): return False


class OwnerScreening:
    def __init__(self,manager):
        self.m=manager
        self.speech=synthesize
        self.tasks={}
        self.briefs={}

    def settings(self):
        s=self.m.settings
        return s.model_copy(update={'owner_speech_model':s.owner_screen_speech_model,
            'owner_speech_voice':s.owner_screen_speech_voice})

    def prepare(self,tid):
        for key,asset in list(self.briefs.items()):
            if asset['expires']<=time.monotonic():self.briefs.pop(key,None)
        for key,task in list(self.tasks.items()):
            if task.done():self.tasks.pop(key,None)
        if not self.m.settings.owner_screen_speech_enabled or tid in self.tasks: return
        if tid in self.briefs:return
        async def render():
            try:
                row=self.m.transfer.get(tid)
                audio=await asyncio.wait_for(self.speech(self.settings(),row['reason']),10)
                latest=self.m.transfer.get(tid)
                if (valid_wav(audio,15) and not latest['decision'] and
                        latest['status'] not in ('completed','returned_to_assistant','unconfirmed')):
                    self.briefs[tid]={'wav':audio,'expires':time.monotonic()+90,'reason':row['reason']}
            except Exception as exc:
                log_event('owner_brief_unavailable',error_type=type(exc).__name__)
        task=self.tasks[tid]=asyncio.create_task(render())
        def finished(done):
            if self.tasks.get(tid) is done:self.tasks.pop(tid,None)
        task.add_done_callback(finished)

    def url(self,row,name):
        return self.m.settings.public_url('/twilio/owner-transfer/'+row['transfer_id']+'/prompt/'+name+'.wav')

    def static(self,name):
        path=self.m.settings.owner_prompt_directory/prompt_key(self.m.settings,name)
        try:
            audio=path.read_bytes()
            return audio if valid_wav(audio) else None
        except OSError:return None

    def append_prompt(self,response,row,name):
        if self.m.settings.owner_screen_speech_enabled and self.static(name):
            response.play(self.url(row,name))
        else:response.say(PROMPTS[name])

    def screen(self,gather,row):
        asset=self.briefs.get(row['transfer_id'])
        if (asset and asset['expires']>time.monotonic() and asset['reason']==row['reason']):
            gather.play(self.url(row,'brief'))
        else:gather.say(row['reason'])
        policy=self.m.recordings.snapshot(row['call_id'])['policy']
        if policy and not policy['stop_requested_at']:self.append_prompt(gather,row,'recorded')
        self.append_prompt(gather,row,'menu')

    def audio_for(self,tid,name):
        row=self.m.transfer.get(tid)
        if self.m.transfer.ended(self.m.db.get(row['call_id'])) or row['status'] in ('completed','returned_to_assistant','unconfirmed'):
            raise KeyError(name)
        if name=='brief':
            asset=self.briefs.get(tid)
            if (not asset or asset['expires']<=time.monotonic() or asset['reason']!=row['reason'] or row['decision']):
                raise KeyError(name)
            return asset['wav']
        if name not in PROMPTS:raise KeyError(name)
        audio=self.static(name)
        if audio is None:raise KeyError(name)
        if name.startswith('connected-') and not row['accepted']:raise KeyError(name)
        if name=='connected-coaching' and row['mode']!='whisper':raise KeyError(name)
        if name=='connected-simple' and row['mode']!='transcript':raise KeyError(name)
        if name=='menu' and row['decision']:raise KeyError(name)
        return audio

    def discard(self,tid):
        self.briefs.pop(tid,None)
        task=self.tasks.pop(tid,None)
        if task and not task.done():task.cancel()

    async def stop(self):
        for task in self.tasks.values():task.cancel()
        await asyncio.gather(*self.tasks.values(),return_exceptions=True)
        self.tasks.clear();self.briefs.clear()
