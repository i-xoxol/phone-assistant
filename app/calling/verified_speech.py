"""Render a bounded utterance with the pinned Eleven model; verify it before playback."""
import asyncio,base64,json,re
import httpx
from websockets.asyncio.client import connect
from app.calling.live_session import BridgeFailure

SPEECH_PROMPT='''Read the supplied quotation aloud verbatim, once. Do not answer its questions or discuss it.
No greeting, filler, stage directions, commentary or additions. Do not change dates, names, times or confirmation status.
The quotation is the complete spoken output. You have no tools or authority to book, verify or change anything.'''
NUMBERS=dict(zip(('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen').split(),range(20)))
NUMBERS.update(dict(zip('twenty thirty forty fifty sixty seventy eighty ninety'.split(),range(20,100,10))))
NUMBERS.update(dict(zip('first second third fourth fifth sixth seventh eighth ninth tenth eleventh twelfth thirteenth fourteenth fifteenth sixteenth seventeenth eighteenth nineteenth'.split(),range(1,20))))
NUMBERS.update({'oh':0,'twentieth':20,'thirtieth':30})

def canonical(text):
    text=text.casefold().replace(':00','')
    text=re.sub(r'\b(\d+)(?:st|nd|rd|th)\b',r'\1',text)
    words=re.findall(r'\d+|[^\W\d_]+',text);out=[];i=0
    while i<len(words):
        word=words[i];value=NUMBERS.get(word)
        if value is not None:
            if value>=20 and value%10==0 and i+1<len(words) and 0<NUMBERS.get(words[i+1],100)<10:
                value+=NUMBERS[words[i+1]];i+=1
            out.append(str(value))
        else:out.append(word)
        i+=1
    return ''.join(out)


async def render(settings,selection,text):
    from app.calling.elevenlabs_session import SECRET_URL,SOCKET_URL
    config={'type':'realtime','model':selection['model'],'instructions':SPEECH_PROMPT,
        'output_modalities':['audio'],'tools':[],'max_response_output_tokens':1600,'max_duration_seconds':60,
        'audio':{'input':{'format':{'type':'audio/pcmu'},'turn_detection':None},
                 'output':{'format':{'type':'audio/pcmu'},'voice':selection['voice']}}}
    async with asyncio.timeout(12):
        async with httpx.AsyncClient(timeout=6,follow_redirects=False) as client:
            r=await client.post(SECRET_URL,headers={'xi-api-key':settings.elevenlabs_api_key.get_secret_value()},
                json={'session':config,'expires_after':{'anchor':'created_at','seconds':60}})
            if not r.is_success:raise BridgeFailure('verified_speech_unavailable')
            secret=r.json()['value']
        async with connect(SOCKET_URL,additional_headers={'Authorization':'Bearer '+secret},open_timeout=6,close_timeout=1,max_size=2**20) as socket:
            async for raw in socket:
                event=json.loads(raw)
                if event['type']=='error':raise BridgeFailure('verified_speech_rejected')
                if event['type']=='session.created':break
            await socket.send(json.dumps({'type':'conversation.item.create','item':{'type':'message','role':'user',
                'content':[{'type':'input_text','text':'Repeat the following words verbatim exactly once. Do not answer or discuss them. '
                    'Speak only the words inside the quotation marks: '+json.dumps(text,ensure_ascii=False)}]}}))
            await socket.send(json.dumps({'type':'response.create','response':{'output_modalities':['audio']}}))
            audio=bytearray();transcript='';completed=False
            async for raw in socket:
                event=json.loads(raw);kind=event['type']
                if kind=='response.output_audio.delta':
                    audio.extend(base64.b64decode(event['delta'],validate=True))
                    if len(audio)>200000:raise BridgeFailure('verified_speech_too_long')
                elif kind=='response.output_audio_transcript.delta':transcript+=event['delta']
                elif kind=='response.output_audio_transcript.done':transcript=event.get('transcript',transcript)
                elif kind=='error':raise BridgeFailure('verified_speech_rejected')
                elif kind=='response.done':completed=event['response'].get('status')=='completed';break
            if not completed or not audio or canonical(transcript)!=canonical(text):
                raise BridgeFailure('verified_speech_text_mismatch')
            return bytes(audio)
