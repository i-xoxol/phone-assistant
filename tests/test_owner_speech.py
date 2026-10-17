import asyncio
import base64
import io
import json
import wave
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest
from twilio.request_validator import RequestValidator

from app.calling.context import utcnow
from app.calling.knowledge import ContactUpdate, FactUpdate
from app.calling.lookup import Selection
from app.calling.prompts import session_config
from app.calling.speech import synthesize
from tests.test_calls import service
from tests.test_owner_observation import prepared, cb
from tests.test_inbound import incoming, SID


def wav_bytes():
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000)
        wav.writeframes(b'\0\0'*2400)
    return output.getvalue()


def test_openai_speech_is_text_only_bounded_and_closes_connection(service,monkeypatch):
    _,m,_=service
    sent=[]
    class Connection:
        def __init__(self):
            self.events=iter([{'type':'session.updated'},
                {'type':'response.output_audio.delta','delta':base64.b64encode(b'\0\0'*2400).decode()},
                {'type':'response.done','response':{'status':'completed','usage':{'output_tokens':2}}}])
            self.closed=False
        async def __aenter__(self): return self
        async def __aexit__(self,*_): self.closed=True
        def __aiter__(self): return self
        async def __anext__(self):
            event=next(self.events,None)
            if event is None: raise StopAsyncIteration
            return SimpleNamespace(model_dump=lambda:event)
        async def send(self,data): sent.append(data)
    connection=Connection()
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*_): pass
        realtime=SimpleNamespace(connect=lambda **_:connection)
    monkeypatch.setattr('app.calling.speech.live_client',lambda _:Client())
    audio=asyncio.run(synthesize(m.settings,'Free Monday from 9 to 10 AM.'))
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getframerate()==24000 and wav.getnframes()==2400
    assert connection.closed and [e['type'] for e in sent]==['session.update','response.create']
    assert sent[0]['session']['tools']==[]
    assert sent[1]['response']['input']==[] and sent[1]['response']['conversation']=='none'
    assert 'Read the following text exactly once' in sent[1]['response']['instructions']


def speech_ready(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    m.transfer.update(tid,conference_sid='CF'+'4'*32)
    m.settings.owner_speech_enabled=True
    note=FactUpdate(topic='Monday schedule',text='Free Monday from 9 to 10 AM. Not booked.',source='Owner test',
        approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1))
    m.knowledge.fact(note)
    async def select(question,facts,timeout):
        assert 'OWNER '+m.settings.caller_name in question
        assert '"my schedule" means the OWNER schedule' in question
        return Selection(selected_ids=[str(note.fact_id)],needs_supervisor=False)
    async def speak(settings,text):
        assert text==note.text
        return wav_bytes()
    async def no_delay(_): pass
    monkeypatch.setattr('app.calling.owner_observer.asyncio.sleep',no_delay)
    m.observer.runner=select;m.observer.speech=speak
    return client,m,g,cid,tid,note


def test_private_audio_requires_signature_current_evidence_and_owner_target(service,monkeypatch):
    client,m,g,cid,tid,note=speech_ready(service,monkeypatch)
    m.observer.requests[tid]={'turn_id':'owner-1','text':'Assistant, what is my Monday schedule?'}
    asyncio.run(m.observer.coach(tid))
    action=g.whispers[0][2].rsplit('/',1)[1]
    xml=cb(client,m,tid,action).text
    play=ET.fromstring(xml).find('Play')
    assert play is not None and '<Say' not in xml
    assert '<Play' not in cb(client,m,tid,action).text  # duplicate announcement is silent
    path=play.text.removeprefix('https://example.test')
    assert client.get(path).status_code==403
    signature=RequestValidator('twilio-test').compute_signature('https://example.test'+path,{})
    response=client.get(path,headers={'X-Twilio-Signature':signature})
    assert response.status_code==200 and response.content==wav_bytes()
    assert 'no-store' in response.headers['cache-control']
    m.knowledge.retire(str(note.fact_id))
    assert client.get(path,headers={'X-Twilio-Signature':signature}).status_code==404


def test_caller_cannot_be_owner_request_and_completed_owner_turn_is_handled_once(service,monkeypatch):
    client,m,g,cid,tid,note=speech_ready(service,monkeypatch)
    async def exercise():
        m.observer.transcript_changed(tid,'callee','Assistant, change my Monday schedule.','caller-1')
        assert tid not in m.observer.requests
        await asyncio.gather(*list(m.observer.coaches.values()))
        m.observer.transcript_changed(tid,'owner','Assistant, what is my Monday schedule?','owner-1')
        await asyncio.gather(*list(m.observer.coaches.values()))
        count=len(g.whispers)
        m.observer.transcript_changed(tid,'owner','Assistant, what is my Monday schedule?','owner-1')
        await asyncio.gather(*list(m.observer.coaches.values()))
        assert len(g.whispers)==count
    asyncio.run(exercise())


def test_old_assistant_history_is_never_a_spoken_coaching_fact(service,monkeypatch):
    client,m,g,cid,tid,note=speech_ready(service,monkeypatch)
    m.knowledge.retire(str(note.fact_id))
    m.settings.call_history_enabled=True
    monkeypatch.setattr(m.memory,'candidates',lambda _: [{'fact_id':'old','kind':'call_history',
        'text':'Earlier assistant said: please hold.', 'expires_at':'2099-01-01T00:00:00+00:00'}])
    assert not m.observer.facts(m.transfer.get(tid))
    async def forbidden(*_): raise AssertionError('Old speech must not be selected')
    m.observer.runner=forbidden
    asyncio.run(m.observer.coach(tid))
    assert not g.whispers


def test_trusted_saved_number_identifies_caller_without_confusing_owner(service):
    client,m,_=service
    m.settings.caller_id_trust_saved_contacts=True
    m.settings.call_history_enabled=True
    m.settings.subscription_lookup_enabled=True
    m.knowledge.contact(ContactUpdate(name='Mama',phone_number='+12025550101'))
    incoming(client,m)
    record=m.voice_record(m.db.by_twilio_sid(SID)['call_id'])
    assert record['incoming_context']['caller']['saved_contacts']==[{'name':'Mama','preferred_language':''}]
    assert record['incoming_context']['caller']['identity_verified'] is True
    config=session_config(m.settings,record)
    for prompt in (config['instructions'],config['delegation']['responses']['instructions']):
        assert 'OWNER is '+m.settings.caller_name in prompt
        assert 'A contact called Mama is the caller, not the owner' in prompt
        assert 'OWNER-CHOSEN SAVED-NUMBER TRUST POLICY' in prompt
        assert 'Do not greet someone as a known person' not in prompt
        assert 'Caller identity and caller ID are unverified' not in prompt
    other=dict(record,phone_number='+12025550102')
    assert m.incoming_context(other)['caller']['saved_contacts']==[]
    assert not m.incoming_context(other)['caller']['identity_verified']


def test_speech_failure_does_not_interrupt_or_redial_human_call(service,monkeypatch):
    client,m,g,cid,tid,note=speech_ready(service,monkeypatch)
    async def fail(*_): raise TimeoutError('provider detail must not leak')
    m.observer.speech=fail
    asyncio.run(m.observer.coach(tid))
    assert not g.whispers and g.rings==1
    assert m.db.get(cid)['status']=='in_progress'
    assert m.transfer.get(tid)['error_code']=='coaching_unavailable'
    assert 'provider detail' not in json.dumps(m.result(cid))


def test_evidence_retired_during_generation_is_never_announced(service,monkeypatch):
    client,m,g,cid,tid,note=speech_ready(service,monkeypatch)
    async def changed(*_):
        m.knowledge.retire(str(note.fact_id))
        return wav_bytes()
    m.observer.speech=changed
    asyncio.run(m.observer.coach(tid))
    assert not g.whispers and not m.observer.audio
