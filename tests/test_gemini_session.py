"""GenAI adapter contract tests; no real credentials or telephony."""
import asyncio
import base64
import json
from types import SimpleNamespace
from contextlib import asynccontextmanager
import numpy as np
import pytest
from pydantic import SecretStr
from google.genai import types
from app.calling.gemini_session import GeminiConnection,gemini_config,started_gemini_connection
from app.calling.streaming_audio import StreamingAudio,encode_pcm,DECODE
from app.calling.live_session import MediaBridge,BridgeFailure
from app.calling.tools import VoiceTools
from app.calling.voice_backends import BackendChoice
from tests.test_calls import service,create
from tests.test_voice_backends import connection as eleven_connection,translated

class Native:
    def __init__(self): self.contents=[];self.inputs=[];self.outputs=[];self.incoming=asyncio.Queue();self.closed=False
    async def send_client_content(self,**data): self.contents.append(data)
    async def send_realtime_input(self,**data): self.inputs.append(data)
    async def send_tool_response(self,**data): self.outputs.append(data)
    async def close(self): self.closed=True;await self.incoming.put(None)
    async def receive(self):
        while True:
            data=await self.incoming.get()
            if data is None:return
            yield types.LiveServerMessage.model_validate(data)
            if (data.get('server_content') or {}).get('turn_complete'):return


def config(tools=()):return {'tools':[{'function_declarations':[{'name':n} for n in tools]}]}
def conn(tools=(),record=None): return GeminiConnection(Native(),config(tools),record or {})
async def translate(c,event):return [e async for e in c.translate(event)]


def test_g711_golden_values_and_decode():
    samples=np.array([-32768,-30000,-1000,-1,0,1,100,1000,30000,32767],dtype=np.int16)
    assert list(encode_pcm(samples))==[0,2,78,126,255,255,242,206,130,128]
    assert list(DECODE[[0,127,128,255]])==[-32124,0,32124,0]


def test_audio_chunk_boundaries_duration_and_continuity():
    samples=(np.sin(np.arange(24000)*2*np.pi*440/24000)*12000).astype('<i2')
    pcm=samples.tobytes();a=StreamingAudio();b=StreamingAudio()
    expected=a.to_twilio(pcm,last=True)
    chunks=[pcm[i:i+137] for i in range(0,len(pcm),137)]
    result=b''.join(b.to_twilio(chunk) for chunk in chunks)+b.to_twilio(b'',last=True)
    assert result==expected and len(result)==8000
    a=StreamingAudio();b=StreamingAudio()
    decoded=a.from_twilio(expected,last=True)
    chunked=b''.join(b.from_twilio(expected[i:i+71]) for i in range(0,8000,71))+b.from_twilio(b'',last=True)
    assert chunked==decoded and len(decoded)==32000
    # No clicks introduced at arbitrary SDK/Twilio boundaries.
    assert np.max(np.abs(np.diff(np.frombuffer(decoded,dtype='<i2').astype(np.int32))))<2500


def test_streaming_resampler_rejects_aliasing_and_bounds():
    def power(freq):
        samples=(np.sin(np.arange(24000)*2*np.pi*freq/24000)*10000).astype('<i2')
        result=StreamingAudio().to_twilio(samples.tobytes(),last=True)
        return np.mean(DECODE[np.frombuffer(result,dtype=np.uint8)].astype(float)[300:-300]**2)
    assert power(6000)<power(1000)*.001
    c=StreamingAudio()
    with pytest.raises(ValueError):c.from_twilio(b'x'*8001)
    with pytest.raises(ValueError):c.to_twilio(b'x'*480001)
    with pytest.raises(ValueError):c.to_twilio(b'x',last=True)


def test_gemini_config_validates_with_official_sdk_and_shared_permissions(service):
    _,m,_=service
    cid=m.db.create_inbound('CA'+'5'*32,'+12025550101',5,10,'concierge')
    c=gemini_config(m.settings,m.voice_record(cid),{'model':'gemini-3.8-live','voice':'Kore'})
    types.LiveConnectConfig.model_validate(c)
    declarations=c['tools'][0]['function_declarations']
    assert 'save_message' in {t['name'] for t in declarations}
    assert 'press_digits' not in {t['name'] for t in declarations}
    assert all(t['behavior']=='BLOCKING' for t in declarations)
    assert 'thinking_config' not in c and 'proactivity' not in c
    assert c['input_audio_transcription']==c['output_audio_transcription']=={}
    assert c['speech_config']['voice_config']['prebuilt_voice_config']['voice_name']=='Kore'
    assert 'untrusted' in c['system_instruction'] and 'never impersonate' in c['system_instruction'].lower()
    assert not c.get('history_config')


def test_resumed_history_is_completed_before_live_input(service,monkeypatch):
    _,m,_=service
    m.settings.gemini_api_key=SecretStr('synthetic-key');m.settings.gemini_live_verified=True
    cid=m.db.create_inbound('CA'+'7'*32,'+12025550101',5,10,'concierge')
    m.db.transcript(cid,{'type':'session.output_transcript.delta','event_id':'earlier',
        'delta':'Earlier assistant answer.','start_ms':1000,'end_ms':1100})
    record=m.voice_record(cid);native=Native();configs=[]
    @asynccontextmanager
    async def connect(**kwargs):
        configs.append(kwargs['config']);yield native
    async def close():pass
    client=SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect),aclose=close))
    monkeypatch.setattr('app.calling.gemini_session.genai.Client',lambda **_:client)
    async def run():
        async with started_gemini_connection(m.settings,m,record,{'model':'gemini-3.8-live','voice':'Kore'}) as (c,_,__):
            assert configs[0]['history_config']=={'initial_history_in_client_content':True}
            assert native.contents==[{'turns':[{'role':'model','parts':[{'text':'Earlier assistant answer.'}]}],
                                     'turn_complete':True}]
            await c.append_note('commentary','Do not repeat the greeting.','startup_greeting_fallback')
            assert len(native.contents)==1
    asyncio.run(run())


def test_missing_credentials_or_verification_blocks_gemini(service):
    c,m,g=service
    assert c.put('/voice/backend',json={'provider':'gemini'}).status_code==409
    m.settings.gemini_api_key=SecretStr('synthetic-key')
    assert c.put('/voice/backend',json={'provider':'gemini'}).status_code==409
    m.settings.gemini_live_verified=True
    assert c.put('/voice/backend',json={'provider':'gemini','voice':'Kore'}).status_code==200
    cid=create(c).json()['call_id']
    assert m.voice_backends.for_call(cid)['provider']=='gemini'
    m.settings.gemini_live_verified=False
    with pytest.raises(BridgeFailure,match='gemini_not_verified'):
        asyncio.run(started_gemini_connection(m.settings,m,m.db.get(cid),m.voice_backends.for_call(cid)).__aenter__())


def test_one_time_silent_greeting_and_roles_for_spoken_update():
    async def run():
        c=conn()
        await c.append_note('commentary','Say hello once.','startup_greeting_fallback')
        await c.append_note('commentary','Say hello again.','startup_greeting_fallback')
        assert len(c.native.contents)==1 and c.native.contents[0]['turn_complete']
        await c.append_note('thinking','Fictional room: Cedar Orchard.','call_update_fact')
        await c.append_note('commentary','Ask the caller whether blue or green.','call_update_question')
        assert c.native.contents[1]['turn_complete'] is False
        assert c.native.contents[2]['turn_complete'] is True
        assert 'TO THE CALLER' in c.native.contents[2]['turns'][0]['parts'][0]['text']
        assert all(t['role']=='user' for r in c.native.contents for t in r['turns'])
        events=[await c.queue.get(),await c.queue.get()]
        assert all(e.type=='session.update_unconfirmed' for e in events)
        assert not any('appended' in e.type for e in events)
        resumed=conn(record={'transcript':[{'speaker':'assistant','text':'Hello'}]})
        await resumed.append_note('commentary','Greet again.','startup_greeting_fallback')
        assert not resumed.native.contents
    asyncio.run(run())


def test_all_parts_and_both_transcript_speakers_in_one_event():
    async def run():
        c=conn();pcm=b'\0\0'*2400
        events=await translate(c,{'server_content':{'model_turn':{'parts':[
            {'inline_data':{'mime_type':'audio/pcm;rate=24000','data':pcm}},
            {'inline_data':{'mime_type':'audio/pcm;rate=24000','data':pcm}}]},
            'input_transcription':{'text':'Caller question'},'output_transcription':{'text':'Assistant answer'},'turn_complete':True}})
        assert len(b''.join(base64.b64decode(e['delta']) for e in events if e['type']=='session.output_audio.delta'))==1600
        assert [(e['type'],e['delta']) for e in events if 'transcript' in e['type']]==[
            ('session.input_transcript.delta','Caller question'),('session.output_transcript.delta','Assistant answer')]
        await c.session.input_audio.append(audio=base64.b64encode(b'\xff'*1600).decode())
        assert c.native.inputs[0]['audio'].mime_type=='audio/pcm;rate=16000'
    asyncio.run(run())


def test_native_interruption_clears_queue_and_discards_same_event_audio():
    async def run():
        c=conn()
        events=await translate(c,{'server_content':{'interrupted':True,
            'model_turn':{'parts':[{'inline_data':{'mime_type':'audio/pcm;rate=24000','data':b'\0\0'*5000}}]},
            'input_transcription':{'text':'Interrupting caller'}}})
        assert [e['type'] for e in events]==['session.output_audio.clear','session.input_transcript.delta']
    asyncio.run(run())


def test_cancelled_or_duplicate_tools_do_not_execute_side_effects(service):
    _,m,_=service;cid=m.db.create_inbound('CA'+'6'*32,'+12025550101',5,10,'messages')
    async def run():
        c=conn(['save_message']);bridge=MediaBridge(None,c,m,cid,'MZ_SYNTHETIC',luna_booking=True);m.attach_bridge(cid,bridge)
        event={'tool_call':{'function_calls':[{'id':'tool-one','name':'save_message','args':{
            'caller_name':'Fictional','message':'Fictional test message.','callback_number':''}}]}}
        for e in await translate(c,event):bridge.tools.handle(e)
        await translate(c,{'tool_call_cancellation':{'ids':['tool-one']}})
        if m.tool_tasks:await asyncio.gather(*list(m.tool_tasks))
        assert not m.db.get(cid)['message_id'] and not m.db.get(cid)['voice_tool_operations']
        assert not await translate(c,event)
        # An independent call executes once through the SAME server permission path.
        event['tool_call']['function_calls'][0]['id']='tool-two'
        for e in await translate(c,event):bridge.tools.handle(e)
        if m.tool_tasks:await asyncio.gather(*list(m.tool_tasks))
        assert m.db.get(cid)['message_id']
        assert len(m.db.get(cid)['voice_tool_operations'])==1
        assert len(c.native.outputs)==1
        assert not await translate(c,event)
        assert not m.tool_tasks
        m.detach_bridge(cid,bridge)
    asyncio.run(run())


def test_multiple_receive_turns_and_clean_finalization():
    async def run():
        c=conn()
        await c.native.incoming.put({'server_content':{'output_transcription':{'text':'First'},'turn_complete':True}})
        await c.native.incoming.put({'server_content':{'output_transcription':{'text':'Second'},'turn_complete':True}})
        iterator=c.__aiter__()
        assert (await anext(iterator)).model_dump()['delta']=='First'
        assert (await anext(iterator)).model_dump()['delta']=='Second'
        await c.close()
        assert (await anext(iterator)).type=='session.closed'
        await iterator.aclose()
    asyncio.run(run())


def test_unconfirmed_update_is_not_provider_acknowledgment(service):
    c,m,_=service;cid=create(c).json()['call_id']
    from app.storage.models import CallUpdateRequest
    async def run():
        native=conn();bridge=MediaBridge(None,native,m,cid,'MZ_SYNTHETIC');m.transition(cid,'in_progress');m.attach_bridge(cid,bridge)
        row=await m.send_call_update(cid,CallUpdateRequest(content='The fictional room is Cedar Orchard.'))
        await bridge.send_pending_updates()
        event=await native.queue.get()
        assert bridge.update_event(event.model_dump())
        assert m.db.call_update(cid,row['update_id'])['status']=='unconfirmed'
        m.detach_bridge(cid,bridge)
    asyncio.run(run())


def test_elevenlabs_opening_signal_is_idempotent():
    async def run():
        c=eleven_connection()
        await c.append_note('commentary','Hello once','startup_greeting_fallback')
        await c.append_note('commentary','Hello twice','startup_greeting_fallback')
        assert sum(e['type']=='response.create' for e in c.socket.sent)==1
        assert sum(e['type']=='conversation.item.create' for e in c.socket.sent)==1
        assert 'startup opening' in c.notes[0]
    asyncio.run(run())
