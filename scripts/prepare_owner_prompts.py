"""Generate the fixed owner keypad/connection clips once. Never places a call."""
import argparse
import asyncio
import json
from pathlib import Path

from app.config import Settings
from app.calling.owner_screening import PROMPTS,prompt_key,valid_wav
from app.calling.speech import synthesize


async def prepare(settings,force=False):
    folder=settings.owner_prompt_directory
    folder.mkdir(parents=True,exist_ok=True)
    speech_settings=settings.model_copy(update={'owner_speech_model':settings.owner_screen_speech_model,
        'owner_speech_voice':settings.owner_screen_speech_voice})
    for name,text in PROMPTS.items():
        path=folder/prompt_key(settings,name)
        if not force and path.exists() and valid_wav(path.read_bytes()):
            print(json.dumps({'prompt':name,'cached':True}));continue
        audio=await synthesize(speech_settings,text,require_transcript=True)
        if not valid_wav(audio,10):raise ValueError('Fixed prompt exceeds ten seconds')
        temporary=path.with_suffix('.tmp')
        temporary.write_bytes(audio)
        temporary.chmod(0o600)
        temporary.replace(path)
        print(json.dumps({'prompt':name,'generated':True,'model':settings.owner_screen_speech_model}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,help='Optional staging output directory')
    parser.add_argument('--env-file',type=Path,help='Read API configuration in place; never copied or printed')
    parser.add_argument('--force',action='store_true',help='Regenerate cached files (paid OpenAI speech)')
    args=parser.parse_args()
    settings=Settings(_env_file=args.env_file) if args.env_file else Settings()
    if args.directory:settings.owner_prompt_directory=args.directory.resolve()
    try:asyncio.run(prepare(settings,args.force))
    except Exception as exc:
        raise SystemExit(f'Owner prompt preparation failed ({type(exc).__name__}). No phone dialed.') from None
