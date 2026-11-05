"""Calendar claims must be text-checked before any telephone playback."""
import asyncio,base64,json
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from pydantic import SecretStr,ValidationError
from app.calling.live_session import BridgeFailure
from app.calling import verified_speech
from app.storage.database import Database
from app.storage.models import CallRequest,CallUpdateRequest
from tests.test_voice_backends import connection,translated,Socket
from tests.test_calls import service
from tests.test_appointments_first_available import first_call
from app.appointments.lookup_intent import LookupIntent
from app.calling.live_session import MediaBridge
from tests.test_call_updates import UpdateConnection


def test_render_checks_full_transcript_before_releasing_any_audio(monkeypatch):
 async def run():
  socket=Socket()
  for event in [{'type':'session.created'},
   {'type':'response.output_audio.delta','delta':base64.b64encode(b'fictional audio').decode()},
   {'type':'response.output_audio_transcript.done','transcript':'Tuesday, November tenth, eleven AM. Does that work?'},
   {'type':'response.done','response':{'status':'completed'}}]:await socket.queue.put(event)
  class Client:
   def __init__(self,**kwargs):pass
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
   async def post(self,*args,**kwargs):
    assert kwargs['json']['session']['tools']==[]
    return SimpleNamespace(is_success=True,json=lambda:{'value':'temporary'})
  @asynccontextmanager
  async def connect(*args,**kwargs):yield socket
  monkeypatch.setattr(verified_speech.httpx,'AsyncClient',Client)
  monkeypatch.setattr(verified_speech,'connect',connect)
  settings=SimpleNamespace(elevenlabs_api_key=SecretStr('fictional'))
  assert await verified_speech.render(settings,{'model':'pinned-model','voice':'pinned-voice'},
   'Tuesday, November 10th, 11:00 AM. Does that work?')==b'fictional audio'
  assert 'Repeat the following words verbatim' in socket.sent[0]['item']['content'][0]['text']
 asyncio.run(run())


@pytest.mark.parametrize('actual',['Booked for two PM.','Tuesday, November 10th, 12 AM. Does that work?',
 'Tuesday, November 10th, 11 AM. Does that work? [confirmed]',''])
def test_render_rejects_extra_or_changed_calendar_claims(monkeypatch,actual):
 async def run():
  socket=Socket()
  for event in [{'type':'session.created'},
   {'type':'response.output_audio.delta','delta':base64.b64encode(b'untrusted audio').decode()},
   {'type':'response.output_audio_transcript.done','transcript':actual},
   {'type':'response.done','response':{'status':'completed'}}]:await socket.queue.put(event)
  class Client:
   def __init__(self,**kwargs):pass
   async def __aenter__(self):return self
   async def __aexit__(self,*args):pass
   async def post(self,*args,**kwargs):return SimpleNamespace(is_success=True,json=lambda:{'value':'temporary'})
  @asynccontextmanager
  async def connect(*args,**kwargs):yield socket
  monkeypatch.setattr(verified_speech.httpx,'AsyncClient',Client);monkeypatch.setattr(verified_speech,'connect',connect)
  with pytest.raises(BridgeFailure,match='verified_speech_text_mismatch'):
   await verified_speech.render(SimpleNamespace(elevenlabs_api_key=SecretStr('fictional')),
    {'model':'pinned','voice':'pinned'},'Tuesday, November 10th, 11 AM. Does that work?')
 asyncio.run(run())


def test_native_hallucinated_booking_is_never_forwarded_after_calendar_control():
 async def run():
  c=connection();c.calendar_controlled=True
  await translated(c,{'type':'response.created','response':{'id':'speculative'}})
  assert not await translated(c,{'type':'response.output_audio.delta','response_id':'speculative','delta':base64.b64encode(b'false booking').decode()})
  assert not await translated(c,{'type':'response.output_audio_transcript.delta','response_id':'speculative','item_id':'i','delta':'You are booked at two PM.'})
 asyncio.run(run())


@pytest.mark.parametrize('case',['verified','new_speech','mismatch','transport_failure'])
def test_actor_only_publishes_current_verified_speech(case):
 async def run():
  c=connection();calls=[];ready=asyncio.Event()
  async def render(text):
   calls.append(text);await ready.wait()
   if case in ('mismatch','transport_failure'):raise BridgeFailure('verified_speech_text_mismatch' if case=='mismatch' else 'unavailable')
   return b'\xff'*800
  c.render_speech=render;c.observe_input_audio(7000,b'\xff'*800)
  await c.speak_verified('Could you remind me of your last name?','call_update_test',lambda:case!='new_speech')
  await asyncio.sleep(0);assert c.local_events.empty()
  ready.set();await asyncio.gather(*list(c.speech_tasks.values()))
  events=[]
  while not c.local_events.empty():events.append(c.local_events.get_nowait())
  audio=[e for e in events if not isinstance(e,Exception) and e.type=='session.output_audio.delta']
  if case=='verified':
   assert len(audio)==1
   speech=events[2].model_dump()
   assert speech['start_ms']==7100 and speech['end_ms']==7200 and speech['speech_source']=='verified_render'
   assert events[0].type=='session.instructions.appended'
  else:
   assert not audio
   if case=='new_speech':assert events[0].type=='session.update_unconfirmed'
   else:assert isinstance(events[-1],BridgeFailure)
  assert len(calls)==(2 if case=='mismatch' else 1)
 asyncio.run(run())


def test_spoken_text_persists_and_cannot_change_under_same_update_id(tmp_path):
 db=Database(tmp_path/'calls.sqlite3');cid=db.create(CallRequest(phone_number='+12025550101',objective='Fictional'))
 request=CallUpdateRequest(content='Ask only for last name.',response_required=True,spoken_text='Your last name, please?')
 db.queue_update(cid,'one',request.content,'instructions',True,request.spoken_text)
 assert Database(db.path).call_update(cid,'one')['spoken_text']==request.spoken_text
 with pytest.raises(ValueError):db.queue_update(cid,'one',request.content,'instructions',True,'Different question')
 for invalid in [' ','я'*201]:
  with pytest.raises(ValidationError):CallUpdateRequest(content='Ask.',spoken_text=invalid)


def test_partial_first_name_requests_only_missing_surname(service,tmp_path):
 client,m,p,g,cid,args=first_call(service,tmp_path)
 c=UpdateConnection();c.config={'model':'eleven_realtime_v1_mini'};c.calendar_controlled=True
 b=MediaBridge(None,c,m,cid,'MZ_PARTIAL',luna_booking=True);m.attach_bridge(cid,b)
 async def decide(_):return LookupIntent(action='clarify',first_name='Casey',last_name='',missing='name')
 g.lookup_intent.decide=decide
 client.portal.call(g.lookup_intent.run,b)
 row=m.db.get(cid)['call_updates'][-1]
 assert row['spoken_text']=='Thanks, Casey. Could you remind me of your last name?'
 assert 'first and last' not in row['spoken_text'] and p.writes==0
