"""Verify real GPT-Live greets a silent caller. Synthetic data; never dials."""
import asyncio
import base64
import json
import re
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.knowledge import ContactUpdate
from app.calling.live_session import has_speech, live_client, start_session
from app.calling.prompts import session_config
from app.storage.database import Database


async def check_one(settings, language, attempt):
    style='familiar' if attempt==1 else 'formal'
    with tempfile.TemporaryDirectory() as folder:
        manager=CallManager(settings,Database(Path(folder)/'calls.sqlite3'),None)
        manager.knowledge.contact(ContactUpdate(name='Mama' if language=='uk' else 'Casey',
            phone_number='+12025550101',preferred_language=language,conversation_style=style,
            preferred_name='Sam' if language=='en' and style=='familiar' else ''))
        cid=manager.db.create_inbound('CA_SILENT_GREETING','+12025550101',5,10,'concierge')
        clock=time.monotonic()
        try:
            async with live_client(settings) as client:
                async with client.live.connect() as connection:
                    await start_session(connection,session_config(settings,manager.voice_record(cid)))
                    async def silence():
                        frame=base64.b64encode(bytes([255])*160).decode()
                        while True:
                            await connection.session.input_audio.append(audio=frame)
                            await asyncio.sleep(.02)
                    feeder=asyncio.create_task(silence())
                    speech_bytes=0
                    transcript=''
                    first_speech=None
                    try:
                        async with asyncio.timeout(15):
                            async for event in connection:
                                data=event.model_dump()
                                if event.type=='session.output_audio.delta':
                                    audio=base64.b64decode(data['delta'],validate=True)
                                    if has_speech(audio):
                                        speech_bytes+=len(audio)
                                        if first_speech is None: first_speech=time.monotonic()-clock
                                elif event.type=='session.output_transcript.delta':
                                    transcript+=data.get('delta','')
                                elif event.type=='error':
                                    raise RuntimeError('Live rejected silent-greeting check')
                                expected=(r'office|офіс|кабінет|приймаль' if style=='formal' else
                                    (r'\bSam\b' if language=='en' else r'привіт|віта|добр'))
                                if speech_bytes>=4000 and re.search(expected,transcript,re.I):
                                    print(json.dumps({'language':language,'style':style,
                                        'silent_caller_greeted':True,'greeting_text_verified':True,
                                        'first_speech_seconds':round(first_speech,2),'telephone_dialed':False}),flush=True)
                                    return
                        raise RuntimeError('Live closed without greeting')
                    finally:
                        feeder.cancel()
                        await asyncio.gather(feeder,return_exceptions=True)
                        await connection.session.close()
        finally:
            await manager.stop_supervision()


async def check():
    settings=Settings(_env_file=Path.cwd()/'.env',caller_id_trust_saved_contacts=True)
    for language in ('en','uk'):
        for attempt in range(1,3):
            await check_one(settings,language,attempt)


if __name__=='__main__':
    try: asyncio.run(check())
    except Exception as exc:
        raise SystemExit('Silent greeting failed ('+type(exc).__name__+'). No phone dialed.') from None
