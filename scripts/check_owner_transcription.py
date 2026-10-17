"""Actual transcription of synthetic PCM16/8k caller speech. No telephone API is used."""
import argparse
import asyncio
import json
import struct
import wave

from app.config import Settings
from app.calling.live_session import live_client
from app.calling.transcription import AudioTurns,configure,open_transcription


async def check(path,expected):
    from scripts.check_owner_transfer_voice import caller_audio
    chunks=caller_audio(path)
    settings=Settings()
    async with live_client(settings) as client:
        async with await open_transcription(client) as connection:
            await configure(connection,settings)
            print(json.dumps({'transcription_session_ready':True}),flush=True)
            text=[]
            async def receive():
                async for event in connection:
                    data=event.model_dump()
                    if data['type']=='error': raise RuntimeError('transcription_provider_error')
                    if data['type']=='conversation.item.input_audio_transcription.completed':
                        text.append(data['transcript'])
                        if expected.lower() in ' '.join(text).lower(): return
            reader=asyncio.create_task(receive())
            try:
                turns=AudioTurns()
                for chunk in chunks+[b'\xff'*160]*40:
                    await turns.feed(connection,chunk)
                    await asyncio.sleep(.02)
                await turns.commit(connection)
                await asyncio.wait_for(reader,15)
                assert expected.lower() in ' '.join(text).lower()
                print(json.dumps({'actual_transcription_verified':True,'no_phone_dialed':True}),flush=True)
            finally:
                reader.cancel();await asyncio.gather(reader,return_exceptions=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--caller-wav',required=True)
    parser.add_argument('--expected-word',default='phone')
    args=parser.parse_args()
    try: asyncio.run(check(args.caller_wav,args.expected_word))
    except Exception as exc:
        raise SystemExit('Synthetic transcription check failed ('+type(exc).__name__+'). No phone dialed.') from None
