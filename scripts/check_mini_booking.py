"""Real Mini + Luna + ASR new booking against an isolated in-memory calendar. Never dials."""
import argparse,asyncio,io,json,re,tempfile,time,wave
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from app.config import Settings
from app.storage.database import Database
from app.calling.call_manager import CallManager
from app.calling.voice_backends import bridge_connection
from app.calling.streaming_audio import StreamingAudio
from app.appointments.gateway import AppointmentGateway
from app.appointments.contracts import Interval,CalendarEvent,EventIdentity
from app.appointments.registry import appointment
from scripts.check_calendar_adapter import NoTelephone,Transport
from tests.appointment_fixtures import FakeProvider

async def check(env_file,audio_dir,busy_first_slot=False):
 source=Settings(_env_file=env_file);clips={}
 for key in ('request','name','details','yes'):
  with wave.open(io.BytesIO((audio_dir/('booking-'+key+'.wav')).read_bytes()),'rb') as w:
   assert w.getframerate()==24000 and w.getnchannels()==1 and w.getsampwidth()==2
   clips[key]=StreamingAudio().to_twilio(w.readframes(w.getnframes()),last=True)
 with tempfile.TemporaryDirectory(prefix='mini-booking-') as folder:
  folder=Path(folder);s=source.model_copy(update={'appointments_enabled':False,'owner_calendar_coaching_enabled':False,
   'owner_observation_enabled':False,'owner_transfer_enabled':False,'appointment_sync_enabled':False,
   'appointments_writes_enabled':True,'call_history_enabled':False,'subscription_lookup_enabled':False,
   'call_recording_enabled':False,'mcp_enabled':False,'caller_name':'Alex','assistant_name':'Assistant',
   'appointment_database_path':folder/'appointments.sqlite3','recording_notice_directory':folder/'notices'})
  m=CallManager(s,Database(folder/'calls.sqlite3'),NoTelephone());clock=lambda:datetime.now(timezone.utc)
  fake=FakeProvider(clock);now=clock().astimezone(ZoneInfo('America/New_York'))
  day=now.date()+timedelta(days=(7-now.weekday())%7 or 7)
  start=datetime.combine(day,datetime.min.time(),tzinfo=ZoneInfo('America/New_York'))+timedelta(hours=10)
  fake.office=replace(fake.office,interval=Interval(start,start+timedelta(hours=2),'America/New_York'))
  if busy_first_slot:
   fake.events['occupied']=CalendarEvent(EventIdentity(fake.identity,'occupied'),'v1',
    Interval(start,start+timedelta(hours=1),'America/New_York'))
  g=AppointmentGateway(m,fake,clock=clock);m.appointments=g;s.appointments_enabled=True;g.name_resolver=None
  cid=m.db.create_inbound('CA_FICTIONAL_BOOKING','+12025550101',3,10,'concierge',True)
  m.db.update(cid,twilio_call_sid='CA_FICTIONAL_BOOKING');m.transition(cid,'in_progress')
  record=m.voice_record(cid);m.voice_backends.pin(record,'elevenlabs',model='eleven_realtime_v1_mini')
  transport=Transport();pending=asyncio.Queue();began=time.monotonic()
  async with asyncio.timeout(160):
   async with bridge_connection(s,m,record) as (con,record,started):
    provider_events=[];original=con.translate
    async def observed(event):
     if event['type'] in ('error','session.updated','response.done','input_audio_buffer.committed'):
      provider_events.append({'type':event['type'],'error':event.get('error'),
       'status':event.get('response',{}).get('status'),
       'turn_detection':event.get('session',{}).get('audio',{}).get('input',{}).get('turn_detection')})
     async for value in original(event):yield value
    con.translate=observed
    m.db.update(cid,openai_session_id=started['session']['id'])
    b=con.bridge_class(transport,con,m,cid,transport.stream,luna_booking=True);m.attach_bridge(cid,b)
    runner=asyncio.create_task(b.run(3))
    async def feed():
     import base64
     buffer=bytearray()
     for tick in range(7500):
      if not buffer and not pending.empty():buffer.extend(pending.get_nowait())
      raw=bytes(buffer[:160]);del buffer[:160];raw+=b'\xff'*(160-len(raw))
      await transport.queue.put({'event':'media','streamSid':transport.stream,'media':{'track':'inbound',
       'timestamp':str(7000+tick*20),'payload':base64.b64encode(raw).decode()}})
      await asyncio.sleep(.02)
    feeder=asyncio.create_task(feed())
    def text():return ''.join(t['text'] for t in m.db.get(cid)['transcript'] if t['speaker']=='assistant').casefold()
    async def until(predicate,seconds=35):
     async with asyncio.timeout(seconds):
      while not predicate():
       if runner.done():runner.result();raise RuntimeError('Bridge ended before booking')
       await asyncio.sleep(.05)
    async def idle():
     # The fake telephone ACKs marks immediately. Still let the caller's audio
     # clock pass the spoken offer, rather than agreeing before hearing it.
     def ready():
      end=max((t['end_timestamp']*1000 for t in m.db.get(cid)['transcript'] if t['speaker']=='assistant'),default=0)
      return (not con.active_response and not con.response_requested and not con.speech_tasks
       and not b.output_busy and not b.queued_bytes and con.input_timeline.stream_end>end+400)
     await until(ready)
    try:
     await until(lambda:transport.audio_bytes>0);greeting_ms=round((time.monotonic()-began)*1000);await idle()
     before=len(text());await pending.put(clips['request']);await until(lambda:len(m.db.get(cid)['call_updates'])>=1)
     await until(lambda:len(text())>before+8);await idle()
     assert con.calendar_controlled and not g.voice_workflow.bound(cid) and fake.writes==0
     assert 'name' in text()[before:] and text()[before:].count('?')<=1
     before=len(text());await pending.put(clips['name']);await until(lambda:len(m.db.get(cid)['call_updates'])>=2)
     await until(lambda:len(text())>before+8);await idle()
     await pending.put(clips['details']);await until(lambda:g.voice_workflow.bound(cid) is not None)
     assert fake.writes==0
     await until(lambda:'monday' in text());await idle()
     value=appointment(g.voice_workflow.bound(cid)[0]);assert value.interval.start>clock()
     assert value.interval.start==start+timedelta(hours=int(busy_first_slot))
     await pending.put(clips['yes']);await until(lambda:fake.writes==1,45)
     await until(lambda:'confirmed' in text() or 'booked' in text());await idle()
     result=await g.voice_workflow.status(cid);assert result['state']=='confirmed' and fake.writes==1
     assert not re.search(r'(?:callback|phone|calling from).{0,30}number',text())
     return {'provider':'elevenlabs_mini','real_audio_asr_luna_and_voice':True,'greeting_ms':greeting_ms,
      'server_controlled_calendar':True,'verified_future_slot_spoken':True,'natural_assent_observed':True,
      'occupied_first_slot_skipped':busy_first_slot,
      'fictional_calendar_writes':fake.writes,'confirmed_result_spoken':True,'real_calendar_accessed':False,
      'telephone_dialed':False,'real_calendar_writes':0}
    finally:
     debug={'provider_events':provider_events,'updates':m.db.get(cid)['call_updates'],'transcript':m.db.get(cid)['transcript'],
      'activity':m.caller_activity.get(cid),'fictional_writes':fake.writes,'controlled':con.calendar_controlled}
     if Path('data').is_dir():Path('data/mini-booking-diagnostic.json').write_text(json.dumps(debug,indent=2))
     feeder.cancel();runner.cancel();await asyncio.gather(feeder,runner,return_exceptions=True)
     m.detach_bridge(cid,b);await g.close();await m.memory.stop()

if __name__=='__main__':
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--env-file',type=Path,required=True);p.add_argument('--audio-dir',type=Path,required=True);p.add_argument('--busy-first-slot',action='store_true');a=p.parse_args()
 try:print(json.dumps(asyncio.run(check(a.env_file,a.audio_dir,a.busy_first_slot))))
 except Exception as e:
  import traceback
  print(json.dumps({'error_type':type(e).__name__,'locations':[(Path(f.filename).name,f.name,f.lineno) for f in traceback.extract_tb(e.__traceback__)[-5:]],'telephone_dialed':False}));raise SystemExit(1)
