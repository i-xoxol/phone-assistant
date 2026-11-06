"""Real-provider synthetic check. Incurs ElevenLabs usage; NEVER dials a phone.

python -m scripts.check_elevenlabs [--input-mulaw path/to/synthetic-8khz.mulaw]
Only fictional context is sent. Credentials come from the private environment.
"""
import argparse
import asyncio
import base64
import json
import tempfile
import time
import wave
from pathlib import Path

from app.calling.call_manager import CallManager
from app.calling.elevenlabs_session import started_elevenlabs_connection
from app.calling.live_session import mulaw_sample
from app.config import ROOT, Settings
from app.storage.database import Database
from app.storage.models import CallUpdateRequest


class NoTelephone:
    async def prepare_inbound(self, *_):
        pass  # Simulate the carrier cap; this object has no carrier client.

    def __getattr__(self, name):
        raise AssertionError('Telephone APIs must not be used by this check')


class SimulatedPhone:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.audio = bytearray()
        self.input = bytearray()

    async def send_json(self, event):
        if event['event'] == 'media':
            self.audio.extend(base64.b64decode(event['media']['payload']))
        elif event['event'] == 'mark':
            await self.queue.put(event)

    async def receive_json(self):
        return await self.queue.get()

    async def feed(self):
        at = 0
        while True:
            raw = bytes(self.input[:160])
            del self.input[:160]
            frame = raw + b'\xff' * (160 - len(raw))
            await self.queue.put({'event':'media', 'streamSid':'MZ_LOCAL_CHECK',
                'media':{'track':'inbound', 'payload':base64.b64encode(frame).decode(), 'timestamp':str(at)}})
            at += 20
            await asyncio.sleep(.02)


async def check(args):
    settings = Settings()
    if not settings.elevenlabs_api_key.get_secret_value() or not settings.elevenlabs_voice_id:
        raise ValueError('Configure ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID')
    # Never load actual caller records or run production background workers.
    settings = settings.model_copy(update=dict(caller_name='Alex', assistant_name='Test Assistant',
        callback_number='', owner_transfer_enabled=False, call_recording_enabled=False,
        appointments_enabled=False, owner_calendar_coaching_enabled=False,
        subscription_lookup_enabled=False, call_history_enabled=False))
    with tempfile.TemporaryDirectory() as folder:
        manager = CallManager(settings, Database(Path(folder)/'check.sqlite3'), NoTelephone())
        cid = manager.db.create_inbound('CA'+'1'*32, '+12025550102', 2, 10, 'concierge', False)
        manager.transition(cid, 'in_progress')
        selection = manager.voice_backends.pin(manager.db.get(cid), 'elevenlabs')
        phone = SimulatedPhone()
        async with started_elevenlabs_connection(settings, manager, manager.db.get(cid), selection) as (conn, _, started):
            manager.db.update(cid, openai_session_id=started['session']['id'])
            bridge = conn.bridge_class(phone, conn, manager, cid, 'MZ_LOCAL_CHECK', luna_booking=True)
            manager.attach_bridge(cid, bridge)
            tool_names = []
            translate = conn.translate
            async def observe(event):
                item = event.get('item', {})
                if event['type']=='response.output_item.done' and item.get('type')=='function_call':
                    tool_names.append(item['name'])
                async for normalized in translate(event):
                    yield normalized
            conn.translate = observe
            conversation = asyncio.create_task(bridge.run(2))
            feeder = asyncio.create_task(phone.feed())

            async def until(predicate, seconds=25):
                async with asyncio.timeout(seconds):
                    while not predicate():
                        if conversation.done():
                            conversation.result()
                            raise RuntimeError('Synthetic bridge closed early')
                        await asyncio.sleep(.02)

            def text(speaker='assistant'):
                return ''.join(e['text'] for e in manager.db.get(cid)['transcript'] if e['speaker']==speaker)

            async def update(mode, content):
                row = await manager.send_call_update(cid, CallUpdateRequest(mode=mode, content=content))
                await until(lambda: manager.db.call_update(cid, row['update_id'])['status']=='acknowledged')

            began = time.monotonic()
            try:
                await until(lambda: bool(phone.audio))
                first_audio_ms = round((time.monotonic()-began)*1000)
                await until(lambda: not conn.active_response and not conn.response_requested)
                await update('context', 'Phone-safe owner update for this synthetic test: the department picnic is Friday.')
                await update('instructions', 'Keep subsequent answers concise and in English. Existing authorization limits still apply.')
                await conn.send({'type':'conversation.item.create', 'item':{
                    'type':'message', 'role':'user', 'content':[{'type':'input_text', 'text':
                    'Please use get_call_context for the current information. When is the department picnic, using the new owner update?'}]}})
                await conn.response.create()
                await until(lambda: 'get_call_context' in tool_names and 'friday' in text().lower())
                await until(lambda: not conn.active_response and not conn.response_requested)
                if 'no mention' in text().lower() or 'not know' in text().lower():
                    raise RuntimeError('Voice did not use the updated synthetic fact')
                tool_count = len(tool_names)
                await update('say', 'Ask whether parking is available at the office.')
                await until(lambda: 'parking' in text().lower())
                await until(lambda: not conn.active_response and not conn.response_requested)
                if len(tool_names) != tool_count:
                    raise RuntimeError('Spoken question was misinterpreted as a tool lookup')
                if args.input_mulaw:
                    phone.input.extend(args.input_mulaw.read_bytes())
                    await until(lambda: bool(text('callee')))
                report = {'no_phone_dialed':True, 'model':selection['model'], 'first_audio_ms':first_audio_ms,
                    'update_modes_acknowledged':[r['mode'] for r in manager.db.get(cid)['call_updates']],
                    'function_calls':tool_names, 'updated_fact_spoken':True,
                    'callee_transcription_verified':bool(text('callee')), 'assistant_text':text(),
                    'callee_text':text('callee')}
            finally:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
                await phone.queue.put({'event':'stop', 'streamSid':'MZ_LOCAL_CHECK'})
                try:
                    await asyncio.wait_for(conversation, 10)
                finally:
                    conversation.cancel()
                    await asyncio.gather(conversation, return_exceptions=True)
                    manager.detach_bridge(cid, bridge)
                    await manager.stop_supervision()
            report['voice_finalized'] = bool(manager.db.get(cid)['voice_finalized'])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(args.output), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
                audio.writeframes(b''.join(mulaw_sample(value).to_bytes(2,'little',signed=True) for value in phone.audio))
            args.output.with_suffix('.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            print(json.dumps(report),flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-mulaw', type=Path, help='Optional fictional speech, raw mono 8 kHz G.711 mu-law, not a WAV')
    parser.add_argument('--output', type=Path, default=ROOT/'data/elevenlabs-check.wav')
    arguments = parser.parse_args()
    try:
        asyncio.run(check(arguments))
    except Exception as exc:
        # Never print HTTP responses, credential-bearing request objects or reprs.
        print(json.dumps({'no_phone_dialed':True,'check':'failed','error_type':type(exc).__name__}),flush=True)
        raise SystemExit(1)
