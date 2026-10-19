"""Verify real GPT-Live greets a silent caller. Synthetic data; never dials."""
import asyncio
import argparse
import base64
import json
import re
import tempfile
import time
from pathlib import Path

from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.knowledge import ContactUpdate
from app.calling.live_session import has_speech, live_client, start_session,GREETING_NUDGE_SECONDS
from app.calling.inbound import startup_greeting_content
from app.calling.prompts import session_config
from app.storage.database import Database


async def check_one(settings, language, attempt,forwarded=False,nudge=False):
    style='familiar' if attempt==1 else 'formal'
    with tempfile.TemporaryDirectory() as folder:
        manager=CallManager(settings,Database(Path(folder)/'calls.sqlite3'),None)
        manager.knowledge.contact(ContactUpdate(name='Mama' if language=='uk' else 'Casey',
            phone_number='+12025550101',preferred_language=language,conversation_style=style,
            preferred_name='Sam' if language=='en' and style=='familiar' else ''))
        cid=manager.db.create_inbound('CA_SILENT_GREETING','+12025550101',5,10,'concierge')
        if forwarded:
            settings.owner_transfer_number='+12025550109'
            settings.inbound_missed_call_context=True
            manager.routing.save(cid,{'ForwardedFrom':settings.owner_transfer_number})
        clock=time.monotonic()
        try:
            async with live_client(settings) as client:
                async with client.live.connect() as connection:
                    record=manager.voice_record(cid)
                    await start_session(connection,session_config(settings,record))
                    async def silence():
                        frame=base64.b64encode(bytes([255])*160).decode()
                        while True:
                            await connection.session.input_audio.append(audio=frame)
                            await asyncio.sleep(.02)
                    feeder=asyncio.create_task(silence())
                    speech_bytes=0
                    transcript=''
                    first_speech=None
                    async def prompt_opening():
                        await asyncio.sleep(GREETING_NUDGE_SECONDS)
                        if not transcript and first_speech is None:
                            content=startup_greeting_content(settings,record)
                            if content:
                                await connection.session.commentary.append(content=content,delegation_id=None,event_id='startup_greeting_fallback')
                    nudger=asyncio.create_task(prompt_opening()) if nudge else None
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
                                    if forwarded and not re.search(r"couldn.t|could not|can.t|не зм|не вдал|не мож|недоступ",transcript,re.I):
                                        continue
                                    print(json.dumps({'language':language,'style':style,
                                        'forwarded':forwarded,
                                        'silent_caller_greeted':True,'greeting_text_verified':True,
                                        'first_speech_seconds':round(first_speech,2),'telephone_dialed':False}),flush=True)
                                    return
                        raise RuntimeError('Live closed without greeting')
                    except TimeoutError:
                        print(json.dumps({'language':language,'style':style,'forwarded':forwarded,
                            'first_speech_seconds':round(first_speech,2) if first_speech else None,
                            'synthetic_greeting':transcript[:600],'telephone_dialed':False},ensure_ascii=False),flush=True)
                        raise
                    finally:
                        if nudger:
                            nudger.cancel()
                            await asyncio.gather(nudger,return_exceptions=True)
                        feeder.cancel()
                        await asyncio.gather(feeder,return_exceptions=True)
                        await connection.session.close()
        finally:
            await manager.stop_supervision()


async def check(forwarded=False,nudge=False):
    settings=Settings(_env_file=Path.cwd()/'.env',caller_id_trust_saved_contacts=True)
    for language in ('en','uk'):
        for attempt in ([2] if forwarded else range(1,3)):
            await check_one(settings,language,attempt,forwarded,nudge)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--forwarded',action='store_true',help='Check missed-call greetings in both languages')
    parser.add_argument('--nudge',action='store_true',default=True,help='Use the production one-time startup fallback (enabled by default)')
    args=parser.parse_args()
    try: asyncio.run(check(args.forwarded,args.nudge))
    except Exception as exc:
        raise SystemExit('Silent greeting failed ('+type(exc).__name__+'). No phone dialed.') from None
