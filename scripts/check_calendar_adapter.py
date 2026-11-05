"""Real voice-provider/ASR/tool loop with a fictional read-only calendar. Never dials."""
import argparse,asyncio,base64,io,json,tempfile,time,wave
from pathlib import Path
from datetime import datetime,timedelta,timezone
from app.config import Settings
from app.storage.database import Database
from app.calling.call_manager import CallManager
from app.calling.voice_backends import bridge_connection
from app.calling.streaming_audio import StreamingAudio
from app.appointments.gateway import AppointmentGateway
from scripts.prepare_recording_notices import render_notice
from tests.appointment_fixtures import FakeProvider

class NoTelephone:
    async def prepare_inbound(self,*_):return
    async def __call__(self,*_):raise AssertionError('No telephone actions allowed')
    def __getattr__(self,_):return self

class Transport:
    def __init__(self):self.queue=asyncio.Queue(maxsize=200);self.stream='MZ_FICTIONAL';self.audio_bytes=0
    async def receive_json(self):return await self.queue.get()
    async def send_json(self,event):
        if event['event']=='media':self.audio_bytes+=len(base64.b64decode(event['media']['payload']))
        elif event['event']=='mark':await self.queue.put(event)

async def check(provider,env_file,caller_audio=None,model=None):
    source=Settings(_env_file=env_file)
    clip=Path(caller_audio).read_bytes() if caller_audio else await render_notice(source,'My name is Casey Example. Please check my appointment.')
    with wave.open(io.BytesIO(clip),'rb') as wav:pcm=wav.readframes(wav.getnframes())
    raw=StreamingAudio().to_twilio(pcm,last=True)
    with tempfile.TemporaryDirectory(prefix='calendar-adapter-') as directory:
        folder=Path(directory)
        s=source.model_copy(update={'appointments_enabled':False,'owner_calendar_coaching_enabled':False,
            'appointments_writes_enabled':False,'appointment_sync_enabled':False,'owner_observation_enabled':False,
            'call_history_enabled':False,'subscription_lookup_enabled':False,'mcp_enabled':False,
            'recording_notice_directory':folder/'notices','appointment_database_path':folder/'appointments.sqlite3',
            'call_recording_enabled':False,'owner_transfer_enabled':False,'caller_name':'Alex','assistant_name':'Assistant'})
        manager=CallManager(s,Database(folder/'calls.sqlite3'),NoTelephone())
        now=datetime.now(timezone.utc);fake=FakeProvider(lambda:now)
        gateway=AppointmentGateway(manager,fake,clock=lambda:now);manager.appointments=gateway;s.appointments_enabled=True
        gateway.name_resolver=None;lookups=[]
        async def read(first,last,*,contradictory=False):
            lookups.append({'name_supplied':bool(first and last),'captions_current':not contradictory})
            if contradictory:return {'status':'needs_owner'}
            return {'status':'match','start':(now+timedelta(days=1)).isoformat(),
                'end':(now+timedelta(days=1,minutes=15)).isoformat(),'zone':'America/New_York',
                'location':'Office A','duration_seconds':900}
        gateway.service.own_appointment=read
        async def forbid(*_,**__):raise AssertionError('Calendar writes forbidden')
        fake.create=forbid
        cid=manager.db.create_inbound('CA_FICTIONAL','+12025550101',2,10,'concierge',False)
        manager.db.update(cid,twilio_call_sid='CA_FICTIONAL');manager.transition(cid,'in_progress')
        record=manager.voice_record(cid);manager.voice_backends.pin(record,provider,model=model)
        transport=Transport()
        async with asyncio.timeout(80):
            async with bridge_connection(s,manager,record) as (connection,record,started):
                manager.db.update(cid,openai_session_id=started['session']['id'])
                provider_events=[]
                original_translate=connection.translate
                async def observed(event):
                    if provider=='elevenlabs':
                        item=event.get('item',{});response=event.get('response',{})
                        provider_events.append({'type':event['type'],'item_type':item.get('type'),
                            'name':item.get('name'),'status':response.get('status'),
                            'output_types':[i.get('type') for i in response.get('output',[])]})
                    async for value in original_translate(event):yield value
                connection.translate=observed
                bridge=connection.bridge_class(transport,connection,manager,cid,transport.stream,luna_booking=True)
                manager.attach_bridge(cid,bridge)
                runner=asyncio.create_task(bridge.run(1))
                async def feed():
                    # A deliberately shifted telephone clock reproduces adapter-origin differences.
                    for tick in range(3500):
                        i=(tick-200)*160
                        payload=raw[i:i+160] if 0<=i<len(raw) else b'\xff'*160
                        await transport.queue.put({'event':'media','streamSid':transport.stream,
                            'media':{'track':'inbound','timestamp':str(7000+tick*20),'payload':base64.b64encode(payload).decode()}})
                        await asyncio.sleep(.02)
                feeder=asyncio.create_task(feed())
                try:
                    async with asyncio.timeout(55):
                        while True:
                            if runner.done():runner.result();raise RuntimeError('Session ended before calendar answer')
                            speech=''.join(t['text'] for t in manager.db.get(cid)['transcript'] if t['speaker']=='assistant').casefold()
                            if 'office a' in speech and any(r['captions_current'] for r in lookups):break
                            await asyncio.sleep(.1)
                    assert fake.writes==0
                    return {'provider':provider,'real_provider_audio_and_caller_asr':True,'shifted_twilio_clock_ms':7000,
                        'calendar_name_and_caption_gates_passed':True,'verified_fictional_calendar_answer_spoken':True,
                        'calendar_source':'fictional_read_only','real_calendar_accessed':False,'calendar_writes':0,'telephone_dialed':False}
                finally:
                    debug={'activity':manager.caller_activity.get(cid),'lookups':lookups,
                        'provider_events':provider_events,'tool_names':list(connection.tool_names) if provider=='elevenlabs' else [],
                        'audio_timeline':list(connection.input_timeline.segments),'speech_times':getattr(connection,'speech_times',{}),
                        'transcript':[{'speaker':t['speaker'],'text':t['text'],'start':t['timestamp'],'end':t['end_timestamp']} for t in manager.db.get(cid)['transcript']]}
                    # Fictional scenario only; this diagnostic is never a production transcript.
                    if Path('data').is_dir():Path('data/'+provider+'-calendar-diagnostic.json').write_text(json.dumps(debug,indent=2))
                    feeder.cancel();runner.cancel();await asyncio.gather(feeder,runner,return_exceptions=True)
                    manager.detach_bridge(cid,bridge);await gateway.close();await manager.memory.stop()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--provider',choices=['gemini','elevenlabs'],required=True);p.add_argument('--env-file',type=Path,required=True);p.add_argument('--caller-audio',type=Path);p.add_argument('--model')
    args=p.parse_args()
    try:print(json.dumps(asyncio.run(check(args.provider,args.env_file,args.caller_audio,args.model))))
    except Exception as exc:
        import traceback
        print(json.dumps({'provider':args.provider,'error_type':type(exc).__name__,
            'locations':[(Path(f.filename).name,f.name,f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-5:]],'telephone_dialed':False}));raise SystemExit(1)
