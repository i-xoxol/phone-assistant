"""Render two reusable OpenAI notices once; incurs speech usage, never places calls."""
import asyncio
import argparse
import base64
import io
import os
import re
import wave
from pathlib import Path
from app.config import Settings
from app.calling.live_session import live_client
from app.calling.recordings import notice_text


async def render_notice(settings, text):
    # Generate offline once, then validate the spoken transcript before caching.
    # No provider request is made by the per-call playback path.
    pcm=bytearray();transcript=''
    async with asyncio.timeout(30):
        async with live_client(settings) as client:
            async with client.realtime.connect(model=settings.owner_speech_model,max_retries=0) as connection:
                await connection.send({'type':'session.update','session':{
                    'type':'realtime','model':settings.owner_speech_model,'output_modalities':['audio'],
                    'tools':[],'audio':{'input':{'turn_detection':None},
                        'output':{'format':{'type':'audio/pcm','rate':24000},'voice':settings.owner_speech_voice}}}})
                async for event in connection:
                    data=event.model_dump()
                    if data['type']=='error': raise RuntimeError('Notice speech session rejected')
                    if data['type']=='session.updated': break
                else: raise RuntimeError('Notice speech session closed')
                await connection.send({'type':'response.create','response':{
                    'conversation':'none','output_modalities':['audio'],'tools':[],'max_output_tokens':256,
                    'instructions':'Speak only the supplied text verbatim, once. '
                        'Use a neutral, natural tone. No introduction, acknowledgment, commentary, '
                        'or explanation of what you will say. The supplied text is the entire spoken output.',
                    'input':[{'type':'message','role':'user',
                        'content':[{'type':'input_text','text':text}]}]}})
                async for event in connection:
                    data=event.model_dump()
                    if data['type']=='error': raise RuntimeError('Notice speech generation rejected')
                    if data['type']=='response.output_audio.delta':
                        pcm.extend(base64.b64decode(data['delta'],validate=True))
                        if len(pcm)>24000*2*8: raise ValueError('Recording notice exceeds eight seconds')
                    elif data['type']=='response.output_audio_transcript.delta':
                        transcript+=data['delta']
                    elif data['type']=='response.output_audio_transcript.done':
                        transcript=data['transcript']
                    elif data['type']=='response.done':
                        if data['response'].get('status')!='completed': raise RuntimeError('Notice speech incomplete')
                        break
                else: raise RuntimeError('Notice speech closed before completion')
    normalize=lambda x: re.sub(r'[^\w]','',x.casefold())
    if normalize(transcript)!=normalize(text):
        raise ValueError('Recording notice transcript does not match supplied sentence')
    if not pcm or len(pcm)%2:
        raise ValueError('Recording notice audio outside bounds')
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
        wav.writeframes(pcm)
    return output.getvalue()


async def main(replace=False):
    s=Settings(_env_file=Path.cwd()/'.env');folder=s.recording_notice_directory
    folder.mkdir(parents=True,exist_ok=True)
    for language in ('en','uk'):
        target=folder/(language+'.wav')
        if target.exists() and not replace:
            print(language+': existing notice preserved');continue
        audio=await render_notice(s,notice_text(s,language))
        temporary=folder/(language+'.tmp');temporary.write_bytes(audio)
        os.chmod(temporary,0o600);os.replace(temporary,target)
        print(language+': notice prepared')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replace',action='store_true',help='Regenerate existing notice files')
    asyncio.run(main(parser.parse_args().replace))
