"""Real Live + subscription Luna recall a synthetic unanswered call. No telephone API."""
import asyncio
import argparse
import base64
import json
import tempfile
from contextlib import suppress
from pathlib import Path

from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge,live_client,start_session
from app.calling.prompts import session_config
from app.storage.database import Database
from app.storage.models import CallRequest
from scripts.check_inbound_voice import NoTelephone,SimulatedPlayback


async def check(wav=None):
    from scripts.check_owner_transfer_voice import caller_audio
    chunks=caller_audio(wav)
    settings=Settings()
    settings.call_history_enabled=True;settings.subscription_lookup_enabled=True
    with tempfile.TemporaryDirectory() as folder:
        m=CallManager(settings,Database(Path(folder)/'check.sqlite3'),NoTelephone())
        old=m.db.create(CallRequest(phone_number='+12025550101',objective='Ask when the blue bicycle will arrive.'))
        m.transition(old,'no_answer')
        cid=m.db.create_inbound('CA_LOCAL_MEMORY','+12025550101',5,10,'concierge')
        m.transition(cid,'in_progress')
        async with live_client(settings) as client:
            async with client.live.connect() as connection:
                await start_session(connection,session_config(settings,m.voice_record(cid)))
                playback=SimulatedPlayback();bridge=MediaBridge(playback,connection,m,cid,'MZ_MEMORY_TEST')
                playback.bridge=bridge;m.attach_bridge(cid,bridge)
                receiver=asyncio.create_task(bridge.receive_live())
                async def silence():
                    index=0;clock=asyncio.get_running_loop().time()
                    while True:
                        audio=b'\xff'*160
                        if asyncio.get_running_loop().time()-clock>=5 and index<len(chunks):
                            audio=chunks[index];index+=1
                        await connection.session.input_audio.append(audio=base64.b64encode(audio).decode())
                        await asyncio.sleep(.02)
                feeder=asyncio.create_task(silence())
                try:
                    await m.lookup.prefetch_history(cid)
                    if not chunks:
                        await connection.response.item.create(item={'type':'message','role':'user','content':[{'type':'input_text',
                            'text':"I am returning the call from this number. What was the call about? Please check the previous call."}]})
                        await connection.response.create()
                    async with asyncio.timeout(45):
                        while True:
                            text=''.join(t['text'] for t in m.db.get(cid)['transcript'] if t['speaker']=='assistant').lower()
                            lookups=m.lookup.list_for_call(cid)
                            acknowledged=any(r['status']=='acknowledged' for r in m.db.get(cid)['call_updates'])
                            if any(r['status']=='answered' for r in lookups) and acknowledged and (not chunks or 'bicycle' in text or 'bike' in text): break
                            if receiver.done(): receiver.result();raise RuntimeError('Live closed before recall')
                            await asyncio.sleep(.1)
                    print(json.dumps({'actual_live_history_injected':True,'actual_subscription_luna':True,
                        'spoken_callback_verified':bool(chunks),'no_phone_dialed':True}),flush=True)
                except Exception:
                    result=m.db.get(cid)
                    print(json.dumps({'lookup_states':[{k:r[k] for k in ('status','error_code','delivery_status')}
                        for r in m.lookup.list_for_call(cid)],
                        'update_states':[r['status'] for r in result['call_updates']],
                        'history_in_update':any('bicycle' in r['content'] for r in result['call_updates']),
                        'voice_operations':result['voice_tool_operations'],'no_phone_dialed':True}),flush=True)
                    raise
                finally:
                    bridge.closing=True
                    feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
                    with suppress(Exception): await connection.session.close()
                    with suppress(TimeoutError): await asyncio.wait_for(bridge.finalized.wait(),5)
                    receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)
                    await m.stop_supervision()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caller-wav',type=Path,help='Optional synthetic callback speech, mono PCM16 at 8 kHz')
    args=parser.parse_args()
    try: asyncio.run(check(args.caller_wav))
    except Exception as exc:
        raise SystemExit('Synthetic callback check failed ('+type(exc).__name__+'). No phone dialed.') from None
