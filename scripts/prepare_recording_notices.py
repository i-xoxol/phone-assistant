"""Render two reusable OpenAI notices once; incurs speech usage, never places calls."""
import asyncio
import os
from pathlib import Path
from app.config import Settings
from app.calling.speech import synthesize
from app.calling.recordings import notice_text


async def main():
    s=Settings(_env_file=Path.cwd()/'.env');folder=s.recording_notice_directory
    folder.mkdir(parents=True,exist_ok=True)
    for language in ('en','uk'):
        target=folder/(language+'.wav')
        if target.exists():
            print(language+': existing notice preserved');continue
        audio=await synthesize(s,notice_text(s,language))
        temporary=folder/(language+'.tmp');temporary.write_bytes(audio)
        os.chmod(temporary,0o600);os.replace(temporary,target)
        print(language+': notice prepared')


if __name__=='__main__': asyncio.run(main())
