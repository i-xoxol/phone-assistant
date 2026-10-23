import asyncio
import base64
from types import SimpleNamespace

import pytest
from app.appointments.fixed_speech import fixed_speech
from app.appointments.evidence import speech_words
from app.config import Settings
from tests.test_live import FakeClient,FakeConnection,Event


class Reader(FakeConnection):
    def __init__(self,text):super().__init__();self.text=text;self.sent=False
    async def append_audio(self,audio):
        if self.sent:return
        self.sent=True
        await self.events.put(Event(type='session.output_audio.delta',delta=base64.b64encode(b'\x10'*1600).decode()))
        await self.events.put(Event(type='session.output_transcript.delta',delta=self.text))


def test_packet_uses_pinned_voice_and_is_validated_before_any_playback(monkeypatch):
    text='Monday, October 26th, 10:00 AM.';reader=Reader(text)
    monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(reader))
    audio=asyncio.run(fixed_speech(Settings(_env_file=None),text,'meridian',lambda t:speech_words(t)==speech_words(text)))
    assert audio==b'\x10'*1600
    assert reader.config['audio']['output']['voice']=='meridian'
    assert reader.config['delegation']=={'type':'client'} and not reader.config['store']
    assert text in reader.config['input'][0]['content'][0]['text']


def test_wrong_date_packet_is_never_returned_for_playback(monkeypatch):
    reader=Reader('Tuesday, October 27th, 10:00 AM.')
    monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(reader))
    async def exercise():
        with pytest.raises(TimeoutError):await asyncio.wait_for(fixed_speech(Settings(_env_file=None),
            'Monday, October 26th, 10:00 AM.','meridian',lambda t:speech_words(t)==speech_words('Monday, October 26th, 10:00 AM.')),.1)
    asyncio.run(exercise())
