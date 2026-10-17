"""Real subscription Luna pre-answer preparation with synthetic contact data. Never dials."""
import asyncio
import json
import tempfile
import time
from pathlib import Path
from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.knowledge import ContactUpdate
from app.calling.prompts import session_config
from app.storage.database import Database


async def check():
    settings=Settings(_env_file=None,subscription_lookup_enabled=True,
        caller_id_trust_saved_contacts=True,inbound_answer_delay_seconds=8)
    with tempfile.TemporaryDirectory() as folder:
        m=CallManager(settings,Database(Path(folder)/'calls.sqlite3'),None)
        m.knowledge.contact(ContactUpdate(name='Mama',phone_number='+12025550101',preferred_language='uk'))
        cid=m.db.create_inbound('CA_SYNTHETIC_GREETING','+12025550101',5,10,'concierge')
        clock=time.monotonic()
        try:
            m.greetings.start(cid)
            await asyncio.gather(*list(m.lookup.tasks.values()))
            config=session_config(settings,m.voice_record(cid))
            assert 'Speak Ukrainian from the very first word' in config['input'][0]['content'][0]['text']
            prepared=bool(m.greetings.snapshot(m.db.get(cid)))
            result={'actual_subscription_luna':prepared,'preparation_status':m.lookup.list_for_call(cid)[0]['status'],
                'elapsed_seconds':round(time.monotonic()-clock,2),'ukrainian_greeting_configured':True,'telephone_dialed':False}
            print(json.dumps(result))
            # A timeout is an expected fallback, not a successful Luna preparation.
            if not prepared: raise RuntimeError('Preparation did not finish within ringing budget')
        finally:
            await m.stop_supervision()


if __name__=='__main__':
    try: asyncio.run(check())
    except Exception as exc: raise SystemExit('Greeting preparation check failed ('+type(exc).__name__+'). No phone dialed.') from None
