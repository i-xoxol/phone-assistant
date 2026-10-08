"""Real GPT-Live append/acknowledgment check with simulated audio; NEVER dials.

Incurs short OpenAI usage. Verifies all three modes plus a spoken fact supplied
through the new MCP-facing manager path. Does not inspect real call transcripts.
"""
import asyncio
import base64
import json
import tempfile
from pathlib import Path

from app.config import ROOT, Settings
from app.storage.database import Database
from app.storage.models import CallRequest, CallUpdateRequest
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge, live_client, start_session
from app.calling.prompts import session_config


class NoTelephone:
    def __getattr__(self, name):
        raise AssertionError('Telephone APIs must not be used by this check')


class SimulatedPlayback:
    bridge = None

    async def send_json(self, message):
        if message['event'] == 'mark':
            self.bridge.pending_marks.pop(message['mark']['name'], None)


async def check():
    settings = Settings()
    settings.live_tools_enabled = False
    with tempfile.TemporaryDirectory() as folder:
        manager = CallManager(settings, Database(Path(folder)/'check.sqlite3'), NoTelephone())
        cid = manager.db.create(CallRequest(phone_number='+12025550101', recipient='Simulated recipient',
            objective='Local context-update check. Speak English. Wait for supervisor updates; keep listening and do not end this test.'))
        manager.transition(cid, 'in_progress')
        async with live_client(settings) as client:
            async with client.live.connect() as connection:
                started = await start_session(connection, session_config(settings, manager.db.get(cid)))
                manager.db.update(cid, openai_session_id=started['session']['id'])
                playback = SimulatedPlayback()
                bridge = playback.bridge = MediaBridge(playback, connection, manager, cid, 'MZ_LOCAL_UPDATES')
                manager.attach_bridge(cid, bridge)
                receiver = asyncio.create_task(bridge.receive_live())
                async def silence():
                    frame = base64.b64encode(bytes([255])*160).decode()
                    while True:
                        await connection.session.input_audio.append(audio=frame)
                        await asyncio.sleep(.02)
                feeder = asyncio.create_task(silence())
                async def acknowledged(row):
                    async with asyncio.timeout(20):
                        while manager.db.call_update(cid, row['update_id'])['status'] != 'acknowledged':
                            current = manager.db.call_update(cid, row['update_id'])
                            if current['status'] in ('rejected','unconfirmed'):
                                raise RuntimeError('Live update was not acknowledged')
                            if receiver.done():
                                receiver.result()
                                raise RuntimeError('Live receiver ended during the check')
                            await asyncio.sleep(.05)
                    print(json.dumps({'append_mode':row['mode'], 'status':'acknowledged'}), flush=True)
                try:
                    context = await manager.send_call_update(cid, CallUpdateRequest(mode='context',
                        content='The only available appointment day is Thursday. The old Wednesday option is unavailable.'))
                    await acknowledged(context)
                    direction = await manager.send_call_update(cid, CallUpdateRequest(mode='instructions',
                        content='Tell the recipient which appointment day is available using the latest supervisor context. Then keep listening.'))
                    await acknowledged(direction)
                    print(json.dumps({'checking_spoken_fact':True}), flush=True)
                    async with asyncio.timeout(20):
                        while 'thursday' not in ''.join(r['text'] for r in manager.db.get(cid)['transcript']
                                                       if r['speaker']=='assistant').lower():
                            if receiver.done():
                                receiver.result()
                                raise RuntimeError('Live ended without using the updated fact')
                            await asyncio.sleep(.05)
                    spoken = await manager.send_call_update(cid, CallUpdateRequest(mode='say',
                        content='Also ask whether parking is available at the office.'))
                    await acknowledged(spoken)
                    print(json.dumps({'live_updates_acknowledged':True, 'modes':['context','instructions','say'],
                        'spoken_updated_fact_verified':True, 'no_phone_dialed':True}))
                except Exception:
                    # Synthetic test speech only; never reads production call records.
                    (ROOT/'data').mkdir(exist_ok=True)
                    (ROOT/'data/call-update-check.json').write_text(json.dumps({
                        'no_phone_dialed':True,
                        'assistant_text':''.join(r['text'] for r in manager.db.get(cid)['transcript']
                                                  if r['speaker']=='assistant')}, ensure_ascii=False), encoding='utf-8')
                    print(json.dumps({'update_states':[{'mode':r['mode'], 'status':r['status'],
                        'error_code':r['error_code']} for r in manager.db.get(cid)['call_updates']],
                        'output_caption_fragments':sum(r['speaker']=='assistant' for r in manager.db.get(cid)['transcript'])}), flush=True)
                    raise
                finally:
                    bridge.closing = True
                    feeder.cancel()
                    await asyncio.gather(feeder, return_exceptions=True)
                    await connection.session.close()
                    try:
                        await asyncio.wait_for(bridge.finalized.wait(), 5)
                    except TimeoutError:
                        pass
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)


if __name__ == '__main__':
    try:
        asyncio.run(check())
    except Exception as exc:
        raise SystemExit(f'Live update check failed ({type(exc).__name__}); no telephone was dialed.') from None
