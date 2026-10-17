"""Actual subscription Luna selects a synthetic fact for a simulated private announcement. Never dials."""
import asyncio
import json
import tempfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.calling.call_manager import CallManager
from app.calling.context import utcnow
from app.calling.knowledge import FactUpdate
from app.storage.database import Database


class NoTelephone:
    async def redirect_call(self,*_): pass
    async def ring_owner(self,*_): return SimpleNamespace(sid='CA'+'3'*32)
    async def whisper(self,cf,sid,url):
        assert cf=='CF'+'4'*32 and sid=='CA'+'3'*32
        self.announcement=url


async def check():
    settings=Settings(_env_file=None,owner_transfer_enabled=True,owner_observation_enabled=True,
        subscription_lookup_enabled=True,owner_transfer_number='+12025550109',
        twilio_phone_number='+12025550100',public_base_url='https://phone.example.com')
    with tempfile.TemporaryDirectory() as folder:
        g=NoTelephone();m=CallManager(settings,Database(Path(folder)/'check.sqlite3'),g)
        cid=m.db.create_inbound('CA_LOCAL_TEST','+12025550101',5,10,'concierge')
        m.transition(cid,'in_progress')
        m.bridges[cid]=SimpleNamespace(closing=False,handoff=False,end_requested=asyncio.Event())
        try:
            tid=(await m.transfer.request(cid,'Synthetic test'))['transfer_id']
            m.transfer.update(tid,status='accepted',accepted=1,decision='accepted',mode='whisper',conference_sid='CF'+'4'*32)
            fact=FactUpdate(topic='Bicycle delivery',text='The blue bicycle arrives Tuesday.',source='Synthetic test',
                approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1))
            m.knowledge.fact(fact)
            m.db.human_caption(cid,'human-synthetic','callee',100,1000,'When does the blue bicycle arrive?')
            await m.observer.coach(tid)
            action=g.announcement.rsplit('/',1)[1]
            xml=await m.observer.callback(m.transfer.get(tid),action,{'CallSid':'CA'+'3'*32})
            assert fact.text in xml
            print(json.dumps({'actual_subscription_luna':True,'private_owner_target_verified':True,
                'telephone_announcement_simulated':True,'no_phone_dialed':True}))
        finally: await m.stop_supervision()


if __name__=='__main__':
    try: asyncio.run(check())
    except Exception as exc:
        raise SystemExit('Synthetic coaching check failed ('+type(exc).__name__+'). No phone dialed.') from None
