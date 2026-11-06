"""Bounded real-provider synthetic voice checks. Never accesses telephone/calendar APIs.

Uses only a fictional SQLite database and simulated playback. Credentials stay in
an owner-supplied environment file. Summaries contain no transcript, private IDs,
recordings, or secrets. --provider gemini does NOT enable it in production.
"""
import argparse
import base64
import asyncio
import json
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge
from app.calling.voice_backends import bridge_connection
from app.storage.database import Database
from app.storage.models import CallUpdateRequest
from app.calling.inbound import startup_greeting_content

class NoTelephone:
    async def prepare_inbound(self,*_):return
    def __getattr__(self,name):raise AssertionError('Telephone API forbidden in synthetic provider check: '+name)

class Playback:
    bridge=None
    def __init__(self):self.audio_frames=0
    async def send_json(self,data):
        if data.get('event')=='media':self.audio_frames+=1
        if data.get('event')=='mark':
            self.bridge.pending_marks.pop(data['mark']['name'],None)
            self.bridge.playback_progress.set()

async def check(provider,env_file,model=None):
    source=Settings(_env_file=env_file)
    with tempfile.TemporaryDirectory(prefix='phone-provider-check-') as directory:
        folder=Path(directory)
        settings=source.model_copy(update={'appointments_enabled':False,'appointments_writes_enabled':False,
            'appointment_sync_enabled':False,'owner_calendar_coaching_enabled':False,'owner_observation_enabled':False,
            'owner_transfer_enabled':False,'call_history_enabled':False,'mcp_enabled':False,
            'caller_name':'Alex','assistant_name':'Assistant','callback_number':'','webhook_url':'',
            'call_recording_enabled':False,'recording_notice_directory':folder/'notices',
            'oauth_database_path':folder/'oauth.sqlite3','gemini_live_verified':True})
        manager=CallManager(settings,Database(folder/'calls.sqlite3'),NoTelephone())
        cid=manager.db.create_inbound('CA_SYNTHETIC_CHECK','+12025550101',2,10,'concierge',False)
        manager.db.update(cid,twilio_call_sid='CA_SYNTHETIC_CHECK')
        manager.transition(cid,'in_progress')
        record=manager.voice_record(cid)
        selection=manager.voice_backends.pin(record,provider,model)
        playback=Playback()
        async with asyncio.timeout(110):
            async with bridge_connection(settings,manager,record) as (connection,record,started):
                manager.db.update(cid,openai_session_id=started['session']['id'])
                bridge=MediaBridge(playback,connection,manager,cid,'MZ_SYNTHETIC_CHECK',luna_booking=True)
                playback.bridge=bridge;manager.attach_bridge(cid,bridge)
                receiver=asyncio.create_task(bridge.receive_live())
                updates=asyncio.create_task(bridge.pump_updates())
                async def silence():
                    frame=base64.b64encode(b'\xff'*160).decode()
                    while True:
                        await connection.session.input_audio.append(audio=frame)
                        await asyncio.sleep(.02)
                feeder=asyncio.create_task(silence())
                async def wait(predicate):
                    async with asyncio.timeout(30):
                        while not predicate():
                            if receiver.done():receiver.result();raise RuntimeError('Provider ended before verification')
                            await asyncio.sleep(.05)
                def spoken_since(index=0):
                    return ''.join(t['text'] for t in manager.db.get(cid)['transcript'][index:] if t['speaker']=='assistant').casefold()
                async def caller(text):
                    if provider=='gemini':
                        await connection.native.send_client_content(turns=[{'role':'user','parts':[{'text':text}]}],turn_complete=True)
                    else:
                        await connection.response.item.create(item={'type':'message','role':'user','content':[{'type':'input_text','text':text}]})
                        await connection.response.create()
                try:
                    await connection.session.commentary.append(content=startup_greeting_content(settings,record),event_id='startup_greeting_fallback',delegation_id=None)
                    await wait(lambda:playback.audio_frames>0 and bool(spoken_since()))
                    await asyncio.sleep(1)
                    first=len(manager.db.get(cid)['transcript'])
                    row=await manager.send_call_update(cid,CallUpdateRequest(mode='context',content='For this fictional check, the meeting room is Cedar Orchard. This fact supersedes any earlier room.'))
                    await wait(lambda:manager.db.call_update(cid,row['update_id'])['status'] in ('acknowledged','unconfirmed'))
                    await caller('I am the simulated caller. What is the fictional meeting room? Please answer briefly.')
                    await wait(lambda:'cedar orchard' in spoken_since(first))
                    fact_audio=playback.audio_frames
                    await asyncio.sleep(1)
                    second=len(manager.db.get(cid)['transcript'])
                    await manager.send_call_update(cid,CallUpdateRequest(mode='say',content='Ask the caller: do you prefer blue or green? Then listen; do not answer for the caller.'))
                    await wait(lambda:'blue' in spoken_since(second) and 'green' in spoken_since(second))
                    assert playback.audio_frames>fact_audio
                    question=spoken_since(second)
                    assert 'prefer' in question or 'would you' in question
                    # Re-sending startup cannot start a second greeting for optional adapters.
                    if provider in ('elevenlabs','gemini'):
                        await connection.session.commentary.append(content='Greet again.',event_id='startup_greeting_fallback',delegation_id=None)
                    return {'provider':provider,'model':selection['model'],'real_provider':True,
                        'synthetic_playback_audio':playback.audio_frames>0,'silent_caller_opening':True,
                        'live_fact_used_in_spoken_answer':True,'spoken_update_asked_caller_question':True,
                        'context_delivery_status':manager.db.call_update(cid,row['update_id'])['status'],
                        'telephone_dialed':False,'calendar_accessed':False,'actual_human_hearing_verified':False}
                finally:
                    bridge.closing=True
                    updates.cancel();feeder.cancel()
                    with suppress(Exception):await asyncio.wait_for(connection.session.close(),5)
                    receiver.cancel();await asyncio.gather(receiver,updates,feeder,return_exceptions=True)
                    if manager.tool_tasks:await asyncio.gather(*list(manager.tool_tasks),return_exceptions=True)
                    manager.detach_bridge(cid,bridge)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',choices=['openai','elevenlabs','gemini'],required=True)
    parser.add_argument('--model');parser.add_argument('--env-file',type=Path,required=True)
    args=parser.parse_args()
    try: print(json.dumps(asyncio.run(check(args.provider,args.env_file,args.model))))
    except Exception as exc: raise SystemExit('Synthetic provider check failed ('+type(exc).__name__+'); no telephone was dialed.') from None
