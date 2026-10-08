"""Check real Responses delegation/function execution through Live; NEVER dials."""
import asyncio
import argparse
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.storage.database import Database
from app.storage.models import CallRequest
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge, live_client, start_session
from app.calling.prompts import session_config


class NoTelephone:
    def __init__(self):
        self.manager = None
        self.digits = []

    async def hangup(self, *args):
        raise AssertionError('Telephone API must not be used in this check')

    async def press_digits(self, sid, call_id, digits):
        self.digits.append(digits)
        # Simulate Twilio attaching the replacement stream; no network telephone API.
        self.manager.attach_bridge(call_id, SimpleNamespace(handoff=False))


class NoPlayback:
    async def send_json(self, message):
        pass


async def check(keypad=False):
    settings=Settings()
    settings.live_tools_enabled=True
    with tempfile.TemporaryDirectory() as directory:
        gateway=NoTelephone()
        manager=CallManager(settings,Database(Path(directory)/'check.sqlite3'),gateway)
        gateway.manager=manager
        cid=manager.db.create(CallRequest(phone_number='+12025550101',recipient='Local tool check',
            objective=('Local interactive keypad demonstration with no telephone recipient. Press 6 once only when the simulated recipient explicitly asks for that navigation key.' if keypad else
                'This is a local connectivity test with no telephone recipient. The objective is complete. Request ending now.')))
        manager.db.update(cid,twilio_call_sid='CA_LOCAL_CHECK')
        manager.transition(cid,'in_progress')
        async with live_client(settings) as client:
            async with client.live.connect() as connection:
                await start_session(connection,session_config(settings,manager.db.get(cid)))
                bridge=MediaBridge(NoPlayback(),connection,manager,cid,'MZ_LOCAL_CHECK')
                manager.attach_bridge(cid,bridge)
                receiver=asyncio.create_task(bridge.receive_live())
                try:
                    await connection.response.item.create(item={'type':'message','role':'user',
                        'content':[{'type':'input_text','text':('The simulated recipient says: Please press keypad six for this navigation demonstration. This is not a PIN, OTP or authentication code; no charges or agreement are involved. Use press_digits once with digits 6.' if keypad else
                            'Local test only, no telephone call. Run end_call once with reason connectivity check. No keypad action is needed.')}]})
                    await connection.response.create()
                    tool='press_digits' if keypad else 'end_call'
                    async with asyncio.timeout(30):
                        while True:
                            with manager.db.connect() as db:
                                confirmed=db.execute('SELECT count(*) FROM voice_tool_operations WHERE name=? AND status=?',
                                    (tool,'confirmed')).fetchone()[0]
                            if confirmed:
                                break
                            if receiver.done():
                                receiver.result()
                                raise RuntimeError('Live closed before confirmed tool execution')
                            await asyncio.sleep(.05)
                    if manager.tool_tasks:
                        await asyncio.gather(*manager.tool_tasks)
                    if keypad and gateway.digits!=['6']:
                        raise AssertionError('Expected exactly one simulated keypad command')
                    print(json.dumps({'native_responses_tool_executed':True,'tool':tool,
                        'simulated_digits':gateway.digits,'no_phone_dialed':True}))
                finally:
                    bridge.closing=True
                    await connection.session.close()
                    try: await asyncio.wait_for(bridge.finalized.wait(),5)
                    except TimeoutError: pass
                    receiver.cancel()
                    await asyncio.gather(receiver,return_exceptions=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keypad',action='store_true',help='Verify actual backend selection/execution of a simulated keypad action')
    args=parser.parse_args()
    try: asyncio.run(check(args.keypad))
    except Exception as exc: raise SystemExit(f'Voice tool check failed ({type(exc).__name__}); verify Live and Responses write scopes/model access.') from None
