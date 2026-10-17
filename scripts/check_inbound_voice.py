"""Actual GPT-Live concierge tools with simulated caller, supervisor and telephone. Never dials."""
import asyncio
import base64
import json
import tempfile
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge, live_client, start_session
from app.calling.prompts import session_config
from app.storage.database import Database
from app.storage.models import SupervisorReply


class NoTelephone:
    async def hangup(self,*_):
        raise AssertionError('No telephone API allowed in this check')


class SimulatedPlayback:
    bridge=None

    async def send_json(self,message):
        if message.get('event')=='mark':
            self.bridge.pending_marks.pop(message['mark']['name'],None)
            self.bridge.playback_progress.set()


async def check():
    settings=Settings()
    with tempfile.TemporaryDirectory() as directory:
        manager=CallManager(settings,Database(Path(directory)/'check.sqlite3'),NoTelephone())
        manager.mcp_events=SimpleNamespace(ready_for=lambda _:True)  # simulated supervisor only
        cid=manager.db.create_inbound('CA_LOCAL_CHECK','+12025550101',5,10,'concierge')
        manager.transition(cid,'in_progress')
        async with live_client(settings) as client:
            async with client.live.connect() as connection:
                await start_session(connection,session_config(settings,manager.db.get(cid)))
                print(json.dumps({'incoming_live_session_started':True}),flush=True)
                playback=SimulatedPlayback()
                bridge=MediaBridge(playback,connection,manager,cid,'MZ_LOCAL_CHECK')
                playback.bridge=bridge
                manager.attach_bridge(cid,bridge)
                receiver=asyncio.create_task(bridge.receive_live())
                updates=asyncio.create_task(bridge.pump_updates())
                async def silence():
                    frame=base64.b64encode(bytes([255])*160).decode()
                    while True:
                        await connection.session.input_audio.append(audio=frame)
                        await asyncio.sleep(.02)
                feeder=asyncio.create_task(silence())
                async def wait_for(predicate):
                    async with asyncio.timeout(35):
                        while not predicate():
                            if receiver.done():
                                receiver.result()
                                raise RuntimeError('Live closed before completion')
                            await asyncio.sleep(.05)
                async def input_text(text):
                    await connection.response.item.create(item={'type':'message','role':'user',
                        'content':[{'type':'input_text','text':text}]})
                    await connection.response.create()
                try:
                    await input_text('Simulated incoming caller Casey asks what time is approved for an appointment. '
                        'Please call request_supervisor to ask for verified availability. No booking or money is authorized.')
                    await wait_for(lambda:bool(manager.db.list_supervisor_requests(call_id=cid)))
                    print(json.dumps({'actual_supervisor_request_received':True}),flush=True)
                    request=manager.db.list_supervisor_requests(call_id=cid)[0]
                    await manager.respond_to_supervisor(request['request_id'],SupervisorReply(
                        response='This simulation has no verified availability. Please take a message.',outcome='needs_owner'))
                    await wait_for(lambda:any(r['status']=='acknowledged' for r in manager.db.get(cid)['call_updates']))
                    print(json.dumps({'supervisor_reply_injected':True}),flush=True)
                    await input_text('Simulated voice readback already occurred: Casey wants the owner to return the call about an appointment. '
                        'Caller confirmed this exact message. Save it now with caller_name Casey and an empty callback number. '
                        'No other action. Keep the voice conversation open for this local check.')
                    await wait_for(lambda:bool(manager.db.get(cid)['message_id']))
                    if manager.tool_tasks:
                        await asyncio.gather(*manager.tool_tasks)
                    print(json.dumps({'actual_live_request_tool':True,'simulated_supervisor_reply_acknowledged':True,
                        'actual_live_message_tool':True,'no_phone_dialed':True,'real_rocky_subscription_tested':False}),flush=True)
                finally:
                    bridge.closing=True
                    feeder.cancel()
                    await asyncio.gather(feeder,return_exceptions=True)
                    with suppress(Exception):
                        await connection.session.close()
                    try: await asyncio.wait_for(bridge.finalized.wait(),5)
                    except TimeoutError: pass
                    for task in (receiver,updates): task.cancel()
                    await asyncio.gather(receiver,updates,return_exceptions=True)
                    await manager.stop_supervision()


if __name__=='__main__':
    try: asyncio.run(check())
    except Exception as exc:
        raise SystemExit(f'Incoming voice check failed ({type(exc).__name__}); check Live/Responses model permissions. No phone dialed.') from None
