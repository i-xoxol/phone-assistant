"""Check real GPT-Live owner greetings with synthetic data; never dials Twilio."""
import argparse
import asyncio
import base64
import json
import re
import tempfile
import time
from pathlib import Path

from app.calling.call_manager import CallManager
from app.calling.context import ProfileUpdate
from app.calling.inbound import startup_greeting_content
from app.calling.knowledge import ContactUpdate
from app.calling.live_session import GREETING_NUDGE_SECONDS,has_speech,live_client,start_session
from app.calling.prompts import session_config
from app.config import Settings
from app.storage.database import Database


async def check_one(settings,language,nudge):
    with tempfile.TemporaryDirectory() as folder:
        manager=CallManager(settings,Database(Path(folder)/'calls.sqlite3'),None)
        manager.knowledge.contact(ContactUpdate(name='Alex Example',phone_number='+12025550101',
            preferred_name='Alex',conversation_style='familiar',preferred_language=language))
        manager.context.set_profile(ProfileUpdate(role='Personal concierge',
            context='For Alex, greet casually without self-introduction. A short hello is enough.'))
        cid=manager.db.create_inbound('CA_OWNER_GREETING_CHECK','+12025550101',5,10,'concierge')
        record=manager.voice_record(cid)
        transcript='';first_speech=None;started=time.monotonic()
        try:
            async with live_client(settings) as client:
                async with client.live.connect() as connection:
                    await start_session(connection,session_config(settings,record))
                    async def silence():
                        frame=base64.b64encode(bytes([255])*160).decode()
                        while True:
                            await connection.session.input_audio.append(audio=frame)
                            await asyncio.sleep(.02)
                    async def fallback():
                        await asyncio.sleep(GREETING_NUDGE_SECONDS)
                        if not transcript and first_speech is None:
                            await connection.session.commentary.append(
                                content=startup_greeting_content(settings,record),delegation_id=None,
                                event_id='startup_greeting_fallback')
                    feeder=asyncio.create_task(silence())
                    nudger=asyncio.create_task(fallback()) if nudge else None
                    try:
                        async with asyncio.timeout(9):
                            async for event in connection:
                                data=event.model_dump()
                                if event.type=='session.output_transcript.delta': transcript+=data.get('delta','')
                                elif event.type=='session.output_audio.delta':
                                    if first_speech is None and has_speech(base64.b64decode(data['delta'],validate=True)):
                                        first_speech=time.monotonic()-started
                                elif event.type=='error': raise RuntimeError('Live greeting rejected')
                    except TimeoutError:
                        pass
                    finally:
                        for task in (feeder,nudger):
                            if task: task.cancel()
                        await asyncio.gather(*[t for t in (feeder,nudger) if t],return_exceptions=True)
                        await connection.session.close()
            if first_speech is None or not re.search(r'Alex|Алекс|Олекс',transcript,re.I):
                raise RuntimeError('Silent owner was not greeted by name')
            if re.search(r'Rocky|assistant|office|помічник|асистент|офіс',transcript,re.I):
                raise RuntimeError('Owner greeting included an unwanted introduction')
            print(json.dumps({'language':language,'startup_fallback_enabled':nudge,'owner_greeted':True,
                'no_self_introduction':True,'synthetic_greeting':transcript.strip(),
                'first_speech_seconds':round(first_speech,2),'telephone_dialed':False},ensure_ascii=False),flush=True)
        finally:
            await manager.stop_supervision()


async def check(language):
    settings=Settings().model_copy(update={'caller_name':'Alex','assistant_name':'Rocky',
        'owner_transfer_number':'+12025550101','caller_id_trust_saved_contacts':True})
    for nudge in (False,True): await check_one(settings,language,nudge)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language',choices=['en','uk'],default='en')
    args=parser.parse_args()
    try: asyncio.run(check(args.language))
    except Exception as exc:
        raise SystemExit('Owner greeting check failed ('+type(exc).__name__+'). No phone dialed.') from None
