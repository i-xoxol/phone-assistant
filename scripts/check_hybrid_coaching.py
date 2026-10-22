"""Paid synthetic OpenAI/ChatGPT checks only. No Twilio client or real call."""
import argparse
import asyncio
import io
import json
import time
import wave
from pathlib import Path
import tempfile
from datetime import timedelta
from types import SimpleNamespace

from app.config import Settings
from app.calling.live_coaching import LiveCoachSession,coaching_context,select_coaching
from app.calling.speech import synthesize


async def check(env_file=None,output=None,skip_luna=False):
    private=Settings(_env_file=env_file) if env_file else Settings()
    settings=Settings(_env_file=None,openai_api_key=private.openai_api_key,
        openai_live_model=private.openai_live_model,openai_voice=private.openai_voice,
        owner_speech_model=private.owner_speech_model,owner_speech_voice=private.owner_speech_voice,
        subscription_lookup_fast=True)
    facts=[{'fact_id':'slot-one','text':'Alex is free Tuesday from 9 to 10 AM. Nothing is booked.',
            'expires_at':'2099-01-01T00:00:00+00:00'},
        {'fact_id':'slot-two','text':'Alex is free Thursday from 2 to 3 PM. Nothing is booked.',
            'expires_at':'2099-01-01T00:00:00+00:00'}]
    question=('OWNER Alex asks: They can do mornings only, but Tuesday may be too soon. '
        'Help me clarify this conflict before promising a time. No booking or other action.')
    if not skip_luna:
        started=time.monotonic()
        decision=await select_coaching(question,facts,25,fast=True)
        assert not set(decision.selected_ids)-{f['fact_id'] for f in facts}
        assert not decision.needs_supervisor and decision.selected_ids
        print(json.dumps({'actual_subscription_luna_fast':True,'delivery':decision.delivery,
            'selection_seconds':round(time.monotonic()-started,2)}),flush=True)
    context=coaching_context('Alex',facts,[{'speaker':'callee','text':'Mornings only, but Tuesday may be too soon.'}],
        {'text':question})
    started=time.monotonic()
    audio=await synthesize(settings,'You are free Tuesday from 9 to 10 AM. Nothing is booked yet.')
    with wave.open(io.BytesIO(audio)) as wav:seconds=wav.getnframes()/wav.getframerate()
    print(json.dumps({'actual_short_hint_tts':True,'seconds':round(seconds,2),
        'prepared_in_seconds':round(time.monotonic()-started,2),'no_phone_dialed':True}),flush=True)
    if output:
        output.mkdir(parents=True,exist_ok=True)
        (output/'tts-hint.wav').write_bytes(audio)
    states=[]
    def state(name,**data):
        states.append(name)
        print(json.dumps({'live_state':name,**data}),flush=True)
    coach=LiveCoachSession(settings,context,state)
    try:
        for index in (1,2):
            started=time.monotonic()
            result=await coach.speak(context)
            with wave.open(io.BytesIO(result['wav'])) as wav:seconds=wav.getnframes()/wav.getframerate()
            print(json.dumps({'actual_gpt_live':True,'hint':index,'seconds':round(seconds,2),
                'prepared_in_seconds':round(time.monotonic()-started,2),'synthetic_text':result['text'],
                'no_phone_dialed':True}),flush=True)
            if output:
                output.mkdir(parents=True,exist_ok=True)
                (output/f'live-hint-{index}.wav').write_bytes(result['wav'])
            context={**context,'owner_request':'Ask about a morning the following week; do not offer Thursday afternoon.'}
            coach.update(context)
    finally:await coach.close()
    assert coach.provider_closed.is_set()
    print(json.dumps({'live_finalized':True,'one_socket_reused':states.count('ready')==1,'no_phone_dialed':True}),flush=True)


async def check_followup(env_file=None,output=None):
    """Replay the factual-answer -> unaddressed complex-follow-up path end to end."""
    from app.calling.call_manager import CallManager
    from app.calling.context import utcnow
    from app.calling.knowledge import FactUpdate
    from app.storage.database import Database
    from scripts.check_owner_coaching import NoTelephone
    private=Settings(_env_file=env_file) if env_file else Settings()
    settings=Settings(_env_file=None,openai_api_key=private.openai_api_key,
        openai_live_model=private.openai_live_model,openai_voice=private.openai_voice,
        owner_speech_model=private.owner_speech_model,owner_speech_voice=private.owner_speech_voice,
        twilio_phone_number='+12025550100',public_base_url='https://phone.example.com',
        owner_transfer_number='+12025550109',owner_transfer_enabled=True,owner_observation_enabled=True,
        subscription_lookup_enabled=True,subscription_lookup_fast=True,
        owner_speech_enabled=True,owner_coaching_strategy='hybrid')
    with tempfile.TemporaryDirectory() as folder:
        gateway=NoTelephone();m=CallManager(settings,Database(Path(folder)/'followup.sqlite3'),gateway)
        cid=m.db.create_inbound('CA_LOCAL_FOLLOWUP_TEST','+12025550101',5,10,'concierge')
        m.transition(cid,'in_progress')
        m.bridges[cid]=SimpleNamespace(closing=False,handoff=False,end_requested=asyncio.Event())
        tid=(await m.transfer.request(cid,'Synthetic follow-up test'))['transfer_id']
        m.transfer.update(tid,status='accepted',accepted=1,decision='accepted',mode='whisper',conference_sid='CF'+'4'*32)
        for topic,text in [
            ('Monday availability','For this test only, Monday availability is 9 to 10 AM and 2 to 3 PM. Nothing is booked.'),
            ('Scheduling constraint','For this test only, the caller can meet only in the morning, and Monday may be too soon.')]:
            m.knowledge.fact(FactUpdate(topic=topic,text=text,source='Synthetic test',
                approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1)))
        try:
            for index,text in enumerate(("Rocky, what's my Monday availability?",
                    'They need mornings, but Monday may be too soon. What should I ask?'),1):
                started=time.monotonic()
                m.db.human_caption(cid,'owner-test-'+str(index),'owner',index*1000,index*1000+500,text)
                m.observer.transcript_changed(tid,'owner',text,'owner-test-'+str(index))
                await asyncio.gather(*list(m.observer.coaches.values()))
                with m.db.connect() as db:
                    count=db.execute('SELECT count(*) FROM owner_whispers').fetchone()[0]
                    live=db.execute('SELECT count(*) FROM owner_coaching_outputs').fetchone()[0]
                assert count==index and live==index-1
                action=gateway.announcement.rsplit('/',1)[1]
                xml=await m.observer.callback(m.transfer.get(tid),action,{'CallSid':'CA'+'3'*32})
                assert '<Play>' in xml
                audio=m.observer.audio_for(tid,action.removeprefix('whisper-'))
                if output:
                    output.mkdir(parents=True,exist_ok=True)
                    (output/f'followup-{index}.wav').write_bytes(audio)
                print(json.dumps({'real_hybrid_followup_step':index,'kind':'tts' if index==1 else 'live',
                    'prepared_in_seconds':round(time.monotonic()-started,2),'no_phone_dialed':True}),flush=True)
        finally:await m.stop_supervision()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,help='Read API configuration in place; never copied or printed')
    parser.add_argument('--output',type=Path,help='Save synthetic audio for listening (ignored data/ recommended)')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--skip-luna',action='store_true',help='Run only the paid Live speech check')
    mode.add_argument('--followup',action='store_true',help='Real Luna/TTS/Live replay of an owner follow-up, never dial')
    args=parser.parse_args()
    try:asyncio.run(check_followup(args.env_file,args.output) if args.followup else check(args.env_file,args.output,args.skip_luna))
    except Exception as exc:
        raise SystemExit(f'Hybrid provider check failed ({type(exc).__name__}). No phone dialed.') from None
