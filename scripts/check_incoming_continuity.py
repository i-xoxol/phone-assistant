"""Check real GPT-Live resumes a synthetic message after a refresh; never dials."""
import argparse
import asyncio
import base64
import json
import re
import tempfile
from pathlib import Path

from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge, has_speech, live_client, start_session
from app.calling.prompts import session_config
from app.config import Settings
from app.storage.database import Database


async def check(env_file):
    # Credentials stay on the trusted operator host. Only synthetic conversations
    # and temporary databases are used; no carrier/calendar operation is invoked.
    settings = Settings(_env_file=env_file).model_copy(update={
        'caller_name':'Alex', 'assistant_name':'Rocky', 'owner_transfer_number':'+12025550109',
        'appointments_enabled':False, 'appointments_writes_enabled':False, 'appointment_sync_enabled':False,
        'owner_calendar_coaching_enabled':False, 'subscription_lookup_enabled':False,
        'call_history_enabled':False, 'call_recording_enabled':False})
    with tempfile.TemporaryDirectory() as folder:
        manager = CallManager(settings, Database(Path(folder)/'calls.sqlite3'), None)
        cid = manager.db.create_inbound('CA_CONTINUITY_PROBE', '+12025550101', 5, 10, 'concierge')
        manager.transition(cid, 'in_progress')
        history = [
            ('assistant', "Hello, you've reached Alex's office. I'm Rocky, his assistant. How can I help?"),
            ('callee', 'This is Casey from downstairs. I would like to leave a message.'),
            ('assistant', 'What would you like me to pass along?'),
            ('callee', 'Please tell Alex the noisy fan is keeping me awake and ask him to switch it off. Can you pass that on? Hello? Are you there?'),
        ]
        for index, (speaker, text) in enumerate(history):
            manager.db.transcript(cid, {'type':'session.'+('output' if speaker=='assistant' else 'input')+'_transcript.delta',
                'event_id':'seed_'+str(index), 'start_ms':index*1000, 'end_ms':(index+1)*1000, 'delta':text})
        config = session_config(settings, manager.voice_record(cid))
        config['input'] += [{'type':'message','role':'assistant' if speaker=='assistant' else 'user',
            'content':[{'type':'output_text' if speaker=='assistant' else 'input_text','text':text}]} for speaker,text in history]
        transcript = ''; audio = False; phase_ack = False; update_ack = False
        try:
            async with live_client(settings) as client:
                async with client.live.connect() as connection:
                    await start_session(connection, config)
                    bridge = MediaBridge(None, connection, manager, cid, 'MZ_CONTINUITY_PROBE')
                    manager.attach_bridge(cid, bridge)
                    await bridge.send_pending_updates()
                    await manager.context_changed()
                    update = manager.db.get(cid)['call_updates'][0]
                    async def silence():
                        frame = base64.b64encode(bytes([255])*160).decode()
                        while True:
                            await connection.session.input_audio.append(audio=frame)
                            await asyncio.sleep(.02)
                    feeder = asyncio.create_task(silence())
                    try:
                        async with asyncio.timeout(12):
                            async for event in connection:
                                data = event.model_dump()
                                if event.type == 'session.output_transcript.delta':transcript += data.get('delta','')
                                elif event.type == 'session.output_audio.delta':
                                    audio = audio or has_speech(base64.b64decode(data['delta'],validate=True))
                                elif event.type == 'session.instructions.appended':
                                    phase_ack |= data.get('client_event_id') == bridge.continuity_event_id
                                    update_ack |= data.get('client_event_id') == 'call_update_'+update['update_id']
                                elif event.type == 'error':raise RuntimeError('Live rejected the continuity check')
                    except TimeoutError:
                        pass
                    finally:
                        feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
                        await connection.session.close()
            continued = bool(re.search(r'fan|noise|noisy|keeping.*awake|switch.*off',transcript,re.I))
            repeated = bool(re.search(r"you.ve reached|office of|I(?:'m| am) Rocky|(?:his|your|Alex.s) assistant|how can I help",transcript,re.I))
            result = {'synthetic_only':True,'telephone_dialed':False,'calendar_writes':0,
                'audio_received':audio,'phase_acknowledged':phase_ack,'context_update_acknowledged':update_ack,
                'current_message_acknowledged':continued,'opening_repeated':repeated,
                'synthetic_reply':transcript.strip()}
            print(json.dumps(result,ensure_ascii=False),flush=True)
            if not (audio and phase_ack and update_ack and continued and not repeated):
                raise RuntimeError('Continuity behavior was not verified')
        finally:
            await manager.stop_supervision()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,default=Path.cwd()/'.env')
    args = parser.parse_args()
    try:asyncio.run(check(args.env_file))
    except Exception as exc:
        raise SystemExit('Incoming continuity check failed ('+type(exc).__name__+'). No phone dialed.') from None
