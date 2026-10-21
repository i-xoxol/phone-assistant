import asyncio
from datetime import timedelta

from app.calling.context import utcnow
from app.calling.knowledge import FactUpdate
from app.calling.live_coaching import CoachingSelection
from tests.test_calls import service
from tests.test_owner_observation import prepared,cb
from tests.test_owner_speech import wav_bytes
import pytest


def ready(service,monkeypatch,delivery='live'):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    m.transfer.update(tid,conference_sid='CF'+'4'*32)
    m.settings.owner_speech_enabled=True;m.settings.owner_coaching_strategy='hybrid'
    note=FactUpdate(topic='Availability',text='Free Tuesday at 9 or Thursday at 2. Nothing is booked.',
        source='Synthetic owner test',approved_for_callers=True,expires_at=utcnow()+timedelta(hours=1))
    m.knowledge.fact(note)
    async def select(*_):return CoachingSelection(selected_ids=[str(note.fact_id)],needs_supervisor=False,delivery=delivery)
    async def speak(*_):return {'text':'Ask which of those two times they prefer.','wav':wav_bytes()}
    async def tts(settings,text):assert text==note.text;return wav_bytes()
    async def no_delay(_):pass
    monkeypatch.setattr('app.calling.owner_observer.asyncio.sleep',no_delay)
    m.observer.coaching_runner=select;m.observer.speech=tts;m.observer.live.speak=speak
    return client,m,g,cid,tid,note


def test_complex_hint_is_private_and_source_changes_invalidate_live_paraphrase(service,monkeypatch):
    client,m,g,cid,tid,note=ready(service,monkeypatch)
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==1 and g.whispers[0][:2]==('CF'+'4'*32,'CA'+'3'*32)
    action=g.whispers[0][2].rsplit('/',1)[1]
    assert '<Play>' in cb(client,m,tid,action).text
    wid=action.removeprefix('whisper-')
    assert m.observer.audio_for(tid,wid)==wav_bytes()
    m.knowledge.retire(str(note.fact_id))
    assert '<Play>' not in cb(client,m,tid,action).text
    try:m.observer.audio_for(tid,wid)
    except KeyError:pass
    else:raise AssertionError('Changed evidence must invalidate generated audio')
    assert cb(client,m,tid,action,CallSid=m.db.get(cid)['twilio_call_sid']).status_code==403


def test_simple_fact_never_opens_live(service,monkeypatch):
    client,m,g,cid,tid,note=ready(service,monkeypatch,delivery='tts')
    async def forbidden(*_):raise AssertionError('A simple hint must not start Live')
    m.observer.live.speak=forbidden
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==1


def test_live_failure_has_one_factual_fallback_and_preserves_human_call(service,monkeypatch):
    client,m,g,cid,tid,note=ready(service,monkeypatch)
    async def fail(*_):raise RuntimeError('private provider details')
    m.observer.live.speak=fail
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==1 and g.rings==1 and m.db.get(cid)['status']=='in_progress'
    assert m.observer.history(cid)[0]['text']==note.text


def test_new_owner_question_supersedes_pending_live_hint(service,monkeypatch):
    client,m,g,cid,tid,note=ready(service,monkeypatch)
    async def replaced(*_):
        m.observer.requests[tid]={'turn_id':'new-question','text':'Rocky, ask about next week instead.'}
        return {'text':'This is an old suggestion.','wav':wav_bytes()}
    m.observer.live.speak=replaced
    asyncio.run(m.observer.coach(tid))
    assert not g.whispers


def test_only_owner_can_pause_coaching_captions_continue_and_owner_can_resume(service,monkeypatch):
    client,m,g,cid,tid,note=ready(service,monkeypatch)
    async def run():
        m.observer.transcript_changed(tid,'owner','Rocky, stop coaching.','owner-stop')
        await asyncio.gather(*list(m.observer.tasks))
        assert tid in m.observer.paused
        m.db.human_caption(cid,'caller-new','callee',2000,3000,'More conversation.')
        m.observer.transcript_changed(tid,'callee','Rocky, resume coaching.','caller-resume')
        assert tid in m.observer.paused and not g.whispers
        assert m.db.get(cid)['transcript'][-1]['text']=='More conversation.'
        m.observer.transcript_changed(tid,'owner','Rocky, what about Tuesday?','owner-resume')
        await asyncio.gather(*list(m.observer.coaches.values()))
        assert tid not in m.observer.paused and len(g.whispers)==1
    asyncio.run(run())


def test_simple_answer_never_runs_either_model_with_hybrid_enabled(service,monkeypatch):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='3')
    m.settings.owner_coaching_strategy='hybrid';m.settings.owner_speech_enabled=True
    async def forbidden(*_):raise AssertionError('No coaching models in simple mode')
    m.observer.coaching_runner=forbidden;m.observer.live.speak=forbidden
    async def run():
        m.observer.transcript_changed(tid,'owner','Rocky, help me.','simple')
        assert not m.observer.coaches
    asyncio.run(run())
    assert not g.whispers


def test_live_reopens_after_idle_only_within_the_per_transfer_budget(service):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    created=[]
    class FakeSession:
        def __init__(self,settings,context,state):
            self.closed=asyncio.Event();created.append(self)
        async def speak(self,context):return {'text':'A short hint.','wav':wav_bytes()}
        async def close(self):self.closed.set()
        def update(self,context):self.latest=context
    pool=m.observer.live;pool.factory=FakeSession
    async def run():
        await pool.speak(tid,[])
        await pool.speak(tid,[])
        assert len(created)==1
        created[-1].closed.set()  # The idle watcher retired the first socket.
        await pool.speak(tid,[])
        assert len(created)==2
        created[-1].closed.set()
        with pytest.raises(ValueError,match='budget'):await pool.speak(tid,[])
        await pool.stop()
    asyncio.run(run())
    assert g.rings==1
