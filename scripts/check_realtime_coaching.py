"""Paid synthetic Realtime 2.1 streaming check. No Twilio client, telephone or Luna."""
import argparse
import asyncio
import json
import io
import time
import wave
from pathlib import Path

from app.config import Settings
from app.calling.live_coaching import pcmu_wav
from app.calling.realtime_coaching import RealtimeCoachSession


async def check(env_file=None,output=None,native=False):
    private=Settings(_env_file=env_file) if env_file else Settings()
    settings=Settings(_env_file=None,openai_api_key=private.openai_api_key)
    states=[]
    def state(name,**data):
        states.append(name)
        print(json.dumps({'realtime_state':name,**data}),flush=True)
    session=RealtimeCoachSession(settings,state)
    facts=[{'fact_id':'monday','text':'Alex is free Monday November 2, 2026 from 9 to 10 AM Eastern. Nothing is booked.',
        'expires_at':'2099-01-01T00:00:00+00:00'},
        {'fact_id':'wednesday','text':'Alex is free Wednesday November 4, 2026 from 2 to 3 PM Eastern. Nothing is booked.',
        'expires_at':'2099-01-01T00:00:00+00:00'}]
    scenarios=[
        ([{'speaker':'callee','text':'What is your availability next Monday?'},
            {'speaker':'owner','text':'Let me check my schedule.'}],None),
        ([{'speaker':'callee','text':'I can only do mornings, but Monday may be too soon.'},
            {'speaker':'owner','text':'Rocky, what should I ask?'}],'What should I ask?'),
        ([{'speaker':'callee','text':'My dog likes playing with tennis balls.'},
            {'speaker':'owner','text':'Mine too, he loves them.'}],None),
    ]
    if native:
        from app.calling.speech import synthesize
        from app.calling.coaching_audio import wav_pcm_to_mulaw
        sentences=[('caller','What is your availability next Monday?'),
            ('owner','Let me check my schedule.'),
            ('caller','Can you tell me what my own availability is next Monday?'),
            ('owner','What is my availability next Monday?')]
        audio_turns=[]
        for speaker,text in sentences:
            rendered=await synthesize(settings,text)
            with wave.open(io.BytesIO(rendered)) as wav:
                raw=wav_pcm_to_mulaw(wav.readframes(wav.getnframes()),wav.getframerate())
            audio_turns.append({'speaker':speaker,'audio':raw})
        scenarios=[([],None,audio_turns[:2]),([],None,[audio_turns[2]]),([],None,[audio_turns[3]])]
    try:
        started=time.monotonic()
        await asyncio.wait_for(session.ready.wait(),15)
        if session.closed.is_set():raise RuntimeError('Provider not ready')
        print(json.dumps({'warmup_seconds':round(time.monotonic()-started,3),'model':'gpt-realtime-2.1'}),flush=True)
        for index,scenario in enumerate(scenarios,1):
            captions,request=scenario[:2];turns=scenario[2] if len(scenario)>2 else None
            audio=bytearray();first=None;started=time.monotonic()
            async def chunk(raw):
                nonlocal first
                if first is None:first=time.monotonic()-started
                audio.extend(raw)
            context={'OWNER':'Alex','now':'2026-11-01T12:00:00+00:00','CURRENT_APPROVED_FACTS':facts,
                'recent_captions':captions,'previous_private_hints':[],
                'explicit_owner_request':request,'trigger':'owner_request' if request else 'consider_proactive_hint'}
            result=await session.ask(context,chunk,turns=turns)
            assert bool(audio)==bool(result['audio_bytes'])
            if not native:assert (audio and result['text']) if index<3 else not audio
            elif index in (1,3):assert audio and result['text']
            elif index==2:assert 'nine' not in result['text'].lower() and '9' not in result['text']
            print(json.dumps({'scenario':index,'first_streamed_audio_seconds':round(first,3) if first else None,
                'generation_seconds':round(time.monotonic()-started,3),'speech_seconds':len(audio)/8000,
                'synthetic_hint':result['text'],'skipped':result['skipped'],
                'no_phone_dialed':True,'no_luna_used':True,'native_labelled_audio':native,
                'no_input_transcription':native}),flush=True)
            if output and audio:
                output.mkdir(parents=True,exist_ok=True)
                (output/f'realtime-hint-{index}.wav').write_bytes(pcmu_wav(audio))
        assert states.count('ready')==1
    finally:await session.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--native',action='store_true',help='Generate synthetic speech and test source-labelled audio with no input captions')
    args=parser.parse_args()
    try:asyncio.run(check(args.env_file,args.output,args.native))
    except Exception as exc:raise SystemExit(f'Realtime check failed ({type(exc).__name__}); no phone dialed.') from None
