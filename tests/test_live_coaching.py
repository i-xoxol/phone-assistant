import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace

import pytest

from app.calling.live_coaching import LiveCoachSession,CoachingSelection,coaching_context
from app.config import Settings


class FakeConnection:
    def __init__(self,text='Ask which of those two times works best.',audio=b'\x00'*2400):
        self.events=asyncio.Queue();self.config=None;self.updates=[];self.audio=audio;self.text=text
        self.session=SimpleNamespace(start=self.start,close=self.close,
            input_audio=SimpleNamespace(append=self.silence),
            commentary=SimpleNamespace(append=self.hint),thinking=SimpleNamespace(append=self.context))
    async def __aenter__(self):return self
    async def __aexit__(self,*_):self.transport_closed=True
    def __aiter__(self):return self
    async def __anext__(self):
        value=await self.events.get()
        return SimpleNamespace(type=value['type'],model_dump=lambda:value)
    async def start(self,session):
        self.config=session
        await self.events.put({'type':'session.started','session':{'id':'synthetic'}})
    async def close(self):
        await self.events.put({'type':'session.usage.updated','usage':{'seconds':3}})
        await self.events.put({'type':'session.closed','reason':'close_requested'})
    async def silence(self,audio):assert base64.b64decode(audio)==b'\xff'*160
    async def context(self,**data):
        self.updates.append(data)
        await self.events.put({'type':'session.thinking.appended','client_event_id':data['event_id']})
    async def hint(self,**data):
        await self.events.put({'type':'session.output_transcript.delta','delta':self.text})
        await self.events.put({'type':'session.output_audio.delta','delta':base64.b64encode(self.audio).decode()})
        await self.events.put({'type':'session.output_audio.delta','delta':base64.b64encode(b'\xff'*1600).decode()})


class FakeClient:
    def __init__(self,connection):self.live=SimpleNamespace(connect=lambda:connection)
    async def __aenter__(self):return self
    async def __aexit__(self,*_):pass


def session(connection,states=None,**settings):
    states=states if states is not None else []
    config=Settings(_env_file=None,**settings)
    return LiveCoachSession(config,{'OWNER':'Alex','CURRENT_APPROVED_FACTS':[]},
        lambda state,**data:states.append((state,data)),lambda _:FakeClient(connection))


def test_live_uses_no_backend_or_microphone_and_outputs_one_bounded_utterance():
    async def run():
        connection=FakeConnection();states=[]
        coach=session(connection,states)
        try:
            result=await coach.speak(coach.context)
            assert result['text']==connection.text
            with wave.open(io.BytesIO(result['wav'])) as wav:
                assert wav.getframerate()==8000 and .3<=wav.getnframes()/8000<1
            assert connection.config['delegation']=={'type':'client'}
            assert connection.config['store'] is False
            assert 'not the incoming CALLER' in connection.config['instructions']
            assert 'No tools or delegations' in connection.config['instructions']
        finally:await coach.close()
        assert coach.closed.is_set() and coach.provider_closed.is_set() and connection.transport_closed
        assert ('usage',{'seconds':3}) in states
    asyncio.run(run())


def test_context_updates_are_acknowledged_and_socket_is_reused():
    async def run():
        connection=FakeConnection();coach=session(connection)
        try:
            await coach.speak(coach.context)
            updated={**coach.context,'recent_captions':[{'speaker':'owner','text':'Prefer the later time.'}]}
            coach.update(updated)
            await coach.speak(updated)
            assert len(connection.updates)==1
            assert 'Prefer the later time' in connection.updates[0]['content']
            assert not coach.acks
        finally:await coach.close()
    asyncio.run(run())


@pytest.mark.parametrize('text,audio',[
    (' '.join(['word']*26)+'.',b'\x00'*2400),
    ('One short sentence.',b'\x00'*72000),
    ('This is an unfinished thought',b'\x00'*2400)],ids=['too-many-words','too-long','unfinished'])
def test_long_or_incomplete_live_coaching_is_discarded_not_cut_mid_sentence(text,audio):
    async def run():
        coach=session(FakeConnection(text,audio))
        try:
            with pytest.raises(ValueError):await coach.speak(coach.context)
        finally:await coach.close()
    asyncio.run(run())


def test_idle_and_total_time_limits_close_even_without_provider_disconnect():
    async def run(total):
        connection=FakeConnection();coach=session(connection)
        if total:coach.settings.owner_live_coaching_max_seconds=.1
        else:coach.settings.owner_live_coaching_idle_seconds=.1
        await asyncio.wait_for(coach.closed.wait(),2)
        assert coach.provider_closed.is_set()
    asyncio.run(run(False));asyncio.run(run(True))


def test_coaching_context_excludes_full_records_and_preserves_speaker_labels():
    turns=[{'speaker':'owner','text':'My availability, please.','phone_number':'SECRET'}]
    data=coaching_context('Alex',[{'fact_id':'a','text':'Free Tuesday.'}],turns)
    assert data['recent_captions']==[{'speaker':'owner','text':'My availability, please.'}]
    assert 'SECRET' not in json.dumps(data)


def test_hybrid_schema_cannot_express_tools_destinations_or_arbitrary_speech():
    assert set(CoachingSelection.model_fields)=={'selected_ids','needs_supervisor','delivery'}
    with pytest.raises(ValueError):
        CoachingSelection(selected_ids=[],needs_supervisor=False,delivery='dial')


def test_unexpected_delegation_closes_coach_without_any_tool_execution():
    async def run():
        connection=FakeConnection()
        async def delegate(**_):await connection.events.put({'type':'session.delegation.created'})
        connection.session.commentary.append=delegate
        coach=session(connection)
        try:
            with pytest.raises(RuntimeError):await coach.speak(coach.context)
        finally:await coach.close()
        assert coach.provider_closed.is_set()
    asyncio.run(run())
