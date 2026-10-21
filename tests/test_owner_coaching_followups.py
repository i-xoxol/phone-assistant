import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.calling.context import utcnow
from app.calling.live_coaching import CoachingSelection
from tests.test_calls import service
from tests.test_owner_observation import prepared,cb
from tests.test_hybrid_coaching import ready


def accepted(service):
    client,m,g,cid,tid=prepared(service)
    cb(client,m,tid,'accept',Digits='1')
    m.settings.owner_coaching_strategy='hybrid'
    return m,tid


def test_latest_turn_waits_through_cooldown_instead_of_being_dropped(service):
    m,tid=accepted(service)
    async def run():
        started=time.monotonic()
        assert await m.observer.wait_for_slot(tid,started-19.96,[])
        assert time.monotonic()-started>=.025
        assert tid not in m.observer.wakeups
    asyncio.run(run())


def test_owner_request_wakes_a_queued_automatic_selection(service):
    m,tid=accepted(service)
    async def run():
        waiting=asyncio.create_task(m.observer.wait_for_slot(tid,time.monotonic()-10,[]))
        m.observer.coaches[tid]=waiting
        while tid not in m.observer.wakeups:await asyncio.sleep(.001)
        m.observer.transcript_changed(tid,'owner','Rocky, help me with this question.','owner-new')
        assert await asyncio.wait_for(waiting,2)
        assert m.observer.requests[tid]['turn_id']=='owner-new'
        assert tid not in m.observer.wakeups
    asyncio.run(run())


@pytest.mark.parametrize('question',[
    'They need mornings, but Monday may be too soon. What should I ask?',
    'What other availability?',
    'Що мені запитати?'])
def test_recent_owner_followup_needs_no_repeated_wakeword_but_caller_cannot_issue_it(service,question):
    m,tid=accepted(service)
    m.observer.owner_addressed[tid]=time.monotonic()
    m.observer.coaches[tid]=SimpleNamespace(done=lambda:False)
    m.observer.transcript_changed(tid,'callee',question,'caller-1')
    assert tid not in m.observer.requests
    m.observer.transcript_changed(tid,'owner',question,'owner-1')
    assert m.observer.requests[tid]=={'turn_id':'owner-1','text':question}


def test_followup_cannot_resume_paused_coaching_or_extend_an_expired_owner_address(service):
    m,tid=accepted(service)
    m.observer.coaches[tid]=SimpleNamespace(done=lambda:False)
    m.observer.owner_addressed[tid]=time.monotonic()
    m.observer.paused.add(tid)
    m.observer.transcript_changed(tid,'owner','What about Tuesday?','owner-1')
    assert tid in m.observer.paused and tid not in m.observer.requests
    m.observer.paused.discard(tid)
    m.observer.owner_addressed[tid]=time.monotonic()-121
    m.observer.transcript_changed(tid,'owner','What about Tuesday?','owner-2')
    assert tid not in m.observer.requests


def test_live_followup_can_use_a_fact_already_read_by_tts_without_repeating_the_tts_hint(service,monkeypatch):
    client,m,g,cid,tid,note=ready(service,monkeypatch,delivery='tts')
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==1
    def expire_cooldown():
        m.observer.attempts[tid]=(1,time.monotonic()-60)
        with m.db.connect() as db:
            db.execute('UPDATE owner_whispers SET created_at=?',((utcnow()-timedelta(seconds=60)).isoformat(),))
    expire_cooldown()
    m.db.human_caption(cid,'owner-followup','owner',1000,2000,'They need mornings, but Tuesday may be too soon. What should I ask?')
    async def select(question,facts,_):
        assert str(note.fact_id) in {f['fact_id'] for f in facts}
        assert 'Earlier private hints' in question
        return CoachingSelection(selected_ids=[str(note.fact_id)],needs_supervisor=False,delivery='live')
    m.observer.coaching_runner=select
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==2
    with m.db.connect() as db:
        assert db.execute('SELECT count(*) FROM owner_coaching_outputs').fetchone()[0]==1
    expire_cooldown()
    async def repeated(*_):return CoachingSelection(selected_ids=[str(note.fact_id)],needs_supervisor=False,delivery='tts')
    m.observer.coaching_runner=repeated
    asyncio.run(m.observer.coach(tid))
    assert len(g.whispers)==2
