"""Real GPT-Live selects owner contact against a fake telephone gateway. Never dials."""
import argparse
import asyncio
import base64
import bisect
import json
import struct
import tempfile
import wave
from contextlib import suppress
from pathlib import Path

from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge,live_client,start_session,mulaw_sample
from app.calling.prompts import session_config
from app.storage.database import Database
from scripts.check_inbound_voice import NoTelephone,SimulatedPlayback


class FakeRedirect(NoTelephone):
    redirected=False

    async def redirect_call(self,sid,xml):
        assert sid=='CA_LOCAL_OWNER_TEST' and '<Dial' in xml and '/screen' in xml
        self.redirected=True


def caller_audio(path):
    if not path: return []
    with wave.open(str(path),'rb') as wav:
        if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate())!=(1,2,8000):
            raise ValueError('Caller WAV must be mono 16-bit PCM at 8000 Hz')
        samples=struct.unpack('<'+'h'*wav.getnframes(),wav.readframes(wav.getnframes()))
    pairs=sorted((mulaw_sample(i),i) for i in range(256))
    levels=[p[0] for p in pairs]
    encoded=bytearray()
    for sample in samples:
        index=bisect.bisect_left(levels,sample)
        index=min(max(index,1),255)
        nearest=min((index-1,index),key=lambda n:abs(levels[n]-sample))
        encoded.append(pairs[nearest][1])
    return [bytes(encoded[n:n+160]).ljust(160,b'\xff') for n in range(0,len(encoded),160)]


async def check(wav=None):
    settings=Settings()
    settings.owner_transfer_enabled=True
    settings.owner_transfer_number='+12025550109'
    settings.openai_voice='meridian'
    chunks=caller_audio(wav)
    with tempfile.TemporaryDirectory() as folder:
        gateway=FakeRedirect()
        m=CallManager(settings,Database(Path(folder)/'check.sqlite3'),gateway)
        cid=m.db.create_inbound('CA_LOCAL_OWNER_TEST','+12025550101',5,10,'concierge')
        m.transition(cid,'in_progress')
        async with live_client(settings) as client:
            async with client.live.connect() as connection:
                config=session_config(settings,m.voice_record(cid))
                if not chunks:
                    config['input'].append({'type':'message','role':'user','content':[{'type':'input_text',
                        'text':'This issue needs the owner personally. Please try reaching them now and connect me. Yes, I agree to hold while you try.'}]})
                await start_session(connection,config)
                print(json.dumps({'meridian_session_started':True}),flush=True)
                playback=SimulatedPlayback()
                bridge=MediaBridge(playback,connection,m,cid,'MZ_OWNER_TEST')
                playback.bridge=bridge
                m.attach_bridge(cid,bridge)
                receiver=asyncio.create_task(bridge.receive_live())
                async def feed():
                    index=0
                    clock=asyncio.get_running_loop().time()
                    silence=bytes([255])*160
                    while True:
                        audio=silence
                        if asyncio.get_running_loop().time()-clock>=6 and index<len(chunks):
                            audio=chunks[index];index+=1
                        await connection.session.input_audio.append(audio=base64.b64encode(audio).decode())
                        await asyncio.sleep(.02)
                feeder=asyncio.create_task(feed())
                try:
                    async with asyncio.timeout(50):
                        while not gateway.redirected:
                            if receiver.done(): receiver.result();raise RuntimeError('Voice closed before owner tool')
                            await asyncio.sleep(.05)
                    if m.tool_tasks: await asyncio.gather(*list(m.tool_tasks))
                    assert m.transfer.for_call(cid)['source']=='voice_backend'
                    print(json.dumps({'voice':'meridian','actual_live_owner_tool':True,
                        'synthetic_spoken_input':bool(chunks),'telephone_redirect_simulated':True,'no_phone_dialed':True}),flush=True)
                finally:
                    bridge.closing=True
                    feeder.cancel();await asyncio.gather(feeder,return_exceptions=True)
                    with suppress(Exception): await connection.session.close()
                    with suppress(TimeoutError): await asyncio.wait_for(bridge.finalized.wait(),5)
                    receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)
                    await m.stop_supervision()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caller-wav',type=Path,help='Optional synthetic caller speech, mono PCM16 8kHz')
    args=parser.parse_args()
    try: asyncio.run(check(args.caller_wav))
    except Exception as exc:
        raise SystemExit(f'Owner voice check failed ({type(exc).__name__}). No real telephone dialed.') from None
