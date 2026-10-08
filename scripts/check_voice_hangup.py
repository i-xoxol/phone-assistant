"""Check real GPT-Live voice-initiated hangup with simulated Twilio; never dials.

Uses the configured OpenAI key and incurs a short Live session's API usage.
"""
import asyncio
import base64
import json
import tempfile
import wave
from array import array
from pathlib import Path
from types import SimpleNamespace

from app.config import ROOT, Settings
from app.storage.database import Database
from app.storage.models import CallRequest
from app.calling.live_session import MediaBridge, live_client, start_session, mulaw_sample
from app.calling.prompts import session_config


class SimulatedTwilio:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.barrier_acked = False

    async def receive_json(self):
        return await self.incoming.get()

    async def send_json(self, message):
        if message['event'] == 'mark':
            if message['mark']['name'] == 'end_call_playback':
                self.barrier_acked = True
            await self.incoming.put(message)

    async def send_silence(self):
        payload = base64.b64encode(bytes([255]) * 160).decode()
        speech_path = ROOT / 'data' / 'local-ending-cue.wav'
        speech = b''
        if speech_path.exists():
            with wave.open(str(speech_path), 'rb') as wav:
                if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (8000, 1, 2):
                    raise ValueError('Test speech must be 8 kHz mono PCM16 WAV')
                samples = array('h', wav.readframes(wav.getnframes()))
            levels = [mulaw_sample(b) for b in range(256)]
            speech = bytes(min(range(256), key=lambda b: abs(levels[b] - sample)) for sample in samples)
        elapsed_frames = 0
        while True:
            # A local WAV simulates a recipient saying the objective is complete.
            position = (elapsed_frames - 400) * 160
            if 0 <= position < len(speech):
                payload = base64.b64encode(speech[position:position + 160]).decode()
            else:
                payload = base64.b64encode(bytes([255]) * 160).decode()
            await self.incoming.put({'event': 'media', 'streamSid': 'MZ_LOCAL_TEST',
                                    'media': {'track': 'inbound', 'payload': payload}})
            elapsed_frames += 1
            await asyncio.sleep(.02)


async def check():
    settings = Settings()
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / 'test.sqlite3')
        request = CallRequest(
            phone_number='+12025550101', recipient='Automated local test', max_duration_minutes=1,
            objective='Demonstrate automatic hangup immediately. This is a local automated test without a telephone recipient. Introduce yourself briefly, then initiate ending the call yourself with your instructed closing phrase. There is no need to wait for a response.',
            context='You are speaking to an automated test with no human replies. The objective is already complete after your introduction.',
            constraints=['End this demonstration promptly with the exact closing phrase from your system instructions. Do not ask questions.'])
        call_id = db.create(request)
        record = db.get(call_id)
        manager = SimpleNamespace(settings=settings, db=db)
        ws = SimulatedTwilio()
        async with live_client(settings) as client:
            async with client.live.connect() as connection:
                await start_session(connection, session_config(settings, record))
                bridge = MediaBridge(ws, connection, manager, call_id, 'MZ_LOCAL_TEST')
                silence = asyncio.create_task(ws.send_silence())
                async def completed_cue():
                    await asyncio.sleep(18)
                    await connection.session.instructions.append(
                        content='The automated test objective is now complete. Initiate ending this call yourself now: say the exact closing phrase from your system instructions and then remain silent. No human reply is needed.',
                        delegation_id=None, event_id='test_objective_complete')
                cue = asyncio.create_task(completed_cue())
                try:
                    await asyncio.wait_for(bridge.run(1), timeout=45)
                except TimeoutError:
                    # Preserve evidence if the model did not produce its closing phrase.
                    pass
                finally:
                    silence.cancel()
                    cue.cancel()
                    await asyncio.gather(silence, cue, return_exceptions=True)
        result = db.get(call_id)
        evidence = {
            'dialed_phone': False,
            'assistant_requested_hangup': bridge.end_requested.is_set(),
            'simulated_playback_acknowledged': ws.barrier_acked,
            'voice_finalized': result['voice_finalized'],
            'closing_transcript_observed': bridge.closing_transcript_seen,
            'farewell_observed': bridge.farewell_seen,
            'assistant_text': ''.join(e['text'] for e in result['transcript'] if e['speaker'] == 'assistant')}
        path = ROOT / 'data' / 'automatic-hangup-live-check.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps({k: v for k, v in evidence.items() if k != 'assistant_text'}))
        if not all((evidence['assistant_requested_hangup'],
                    evidence['simulated_playback_acknowledged'], evidence['voice_finalized'],
                    evidence['farewell_observed'])):
            raise RuntimeError('Automatic hangup not confirmed')


if __name__ == '__main__':
    try:
        asyncio.run(check())
    except Exception as exc:
        print(f'Voice hangup check failed ({type(exc).__name__})')
        raise SystemExit(1) from None
