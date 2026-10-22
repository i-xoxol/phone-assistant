import asyncio
import base64
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.config import Settings
from app.calling.realtime_coaching import RealtimeCoachSession,POLICY


class Connection:
    def __init__(self):
        self.queue=asyncio.Queue();self.sent=[];self.counter=0;self.complete=asyncio.Event()
        self.complete.set()
    async def __aenter__(self):return self
    async def __aexit__(self,*_):pass
    def __aiter__(self):return self
    async def __anext__(self):
        data=await self.queue.get()
        return SimpleNamespace(model_dump=lambda:data)
    async def send(self,data):
        self.sent.append(data)
        if data['type']=='session.update':await self.queue.put({'type':'session.updated'})
        if data['type']=='response.create':
            self.counter+=1;rid='resp-'+str(self.counter)
            await self.queue.put({'type':'response.created','response':{'id':rid,'metadata':data['response']['metadata']}})
            await self.queue.put({'type':'response.output_audio.delta','response_id':rid,
                'delta':base64.b64encode(b'\x80'*320).decode()})
            await self.queue.put({'type':'response.output_audio_transcript.done','response_id':rid,
                'transcript':'You are free Monday morning.'})
            async def finish():
                await self.complete.wait()
                await self.queue.put({'type':'response.done','response':{'id':rid,'status':'completed','output':[]}})
            asyncio.create_task(finish())


def factory(connection):
    @asynccontextmanager
    async def client(_):
        yield SimpleNamespace(realtime=SimpleNamespace(connect=lambda **kwargs:connection))
    return client


def test_streams_before_response_finishes_and_reuses_one_full_realtime_connection():
    async def run():
        connection=Connection();connection.complete.clear();audio=asyncio.Event();states=[]
        session=RealtimeCoachSession(Settings(_env_file=None),lambda state,**_:states.append(state),factory(connection))
        async def chunk(raw):assert raw==b'\x80'*320;audio.set()
        task=asyncio.create_task(session.ask({'CURRENT_APPROVED_FACTS':[]},chunk))
        try:
            await asyncio.wait_for(audio.wait(),1)
            assert not task.done()  # Private stream is usable before a full clip exists.
            connection.complete.set()
            assert (await task)['text']=='You are free Monday morning.'
            await session.ask({'CURRENT_APPROVED_FACTS':[{'text':'Updated fact'}]},chunk)
            assert states.count('ready')==1
            setup=connection.sent[0]['session']
            assert setup['model']=='gpt-realtime-2.1' and setup['reasoning']=={'effort':'low'}
            assert setup['audio']['output']['format']=={'type':'audio/pcmu'}
            assert [t['name'] for t in setup['tools']]==['skip_hint']
            assert not any(e['type']=='input_audio_buffer.append' for e in connection.sent)
            requests=[e['response'] for e in connection.sent if e['type']=='response.create']
            assert all(e['conversation']=='none' for e in requests)
            assert 'Updated fact' in json.dumps(requests[-1]['input'])
        finally:await session.close()
    asyncio.run(run())


def test_cancelled_response_cannot_send_late_audio_into_next_owner_request():
    async def run():
        connection=Connection();connection.complete.clear();first=asyncio.Event();packets=[]
        session=RealtimeCoachSession(Settings(_env_file=None),lambda *_,**__:None,factory(connection))
        async def audio(raw):packets.append(raw);first.set()
        task=asyncio.create_task(session.ask({},audio))
        try:
            await asyncio.wait_for(first.wait(),1)
            await session.cancel()
            await connection.queue.put({'type':'response.output_audio.delta','response_id':'resp-1',
                'delta':base64.b64encode(b'late').decode()})
            try:await task
            except asyncio.CancelledError:pass
            connection.complete.set()
            await session.ask({},audio)
            assert b'late' not in packets
            assert any(e['type']=='response.cancel' for e in connection.sent)
        finally:await session.close()
    asyncio.run(run())


def test_policy_covers_both_speakers_proactive_cues_owner_identity_and_limits():
    for text in ('separately labelled audio items','APPLICATION MICROPHONE LABEL','caller asks the owner for','let me check/think',
            'You do not need a wake word','CURRENT_APPROVED_FACTS','never address the caller',
            'You cannot book','No action tools'):
        assert text in POLICY


def test_native_audio_has_separate_application_labels_and_needs_no_transcription():
    async def run():
        connection=Connection();session=RealtimeCoachSession(Settings(_env_file=None),lambda *_,**__:None,factory(connection))
        async def discard(_):pass
        try:
            await session.ask({'recent_captions':[]},discard,turns=[
                {'speaker':'caller','audio':b'caller-raw-audio'},
                {'speaker':'owner','audio':b'owner-raw-audio'}])
            response=next(e['response'] for e in connection.sent if e['type']=='response.create')
            caller,owner=response['input'][1:]
            assert 'LABEL: CALLER' in caller['content'][0]['text']
            assert 'LABEL: OWNER' in owner['content'][0]['text']
            assert base64.b64decode(caller['content'][1]['audio'])==b'caller-raw-audio'
            assert base64.b64decode(owner['content'][1]['audio'])==b'owner-raw-audio'
            assert 'transcription' not in str(connection.sent)
        finally:await session.close()
    asyncio.run(run())
