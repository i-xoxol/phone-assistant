"""Paid synthetic OpenAI/ChatGPT checks only. No Twilio client or real call."""
import argparse
import asyncio
import io
import json
import time
import wave
from pathlib import Path

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


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,help='Read API configuration in place; never copied or printed')
    parser.add_argument('--output',type=Path,help='Save synthetic audio for listening (ignored data/ recommended)')
    parser.add_argument('--skip-luna',action='store_true',help='Run only the paid Live speech check')
    args=parser.parse_args()
    try:asyncio.run(check(args.env_file,args.output,args.skip_luna))
    except Exception as exc:
        raise SystemExit(f'Hybrid provider check failed ({type(exc).__name__}). No phone dialed.') from None
