"""Relaxed caller mentions use fast Luna, while Google reads and booking stay bounded."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock

import pytest

from app.appointments import name_match
from app.appointments.name_match import NameChoice,NameProof
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,agree,speech


def rows(*pairs):
    return [dict(speaker=s,text=t,timestamp=i,end_timestamp=i+.5) for i,(s,t) in enumerate(pairs)]


def test_audio_grader_counts_a_natural_name_question_without_losing_its_question_mark():
    from scripts.simulate_appointment_lookup import name_question_quality
    events=[dict(side='assistant',direction='output',text='Okay, could I get your first and last name, please?',start_ms=0,end_ms=1000)]
    assert name_question_quality(events)['name_questions']['full']==1


def model(monkeypatch,choice=None,error=None):
    parse=AsyncMock(return_value=SimpleNamespace(output_parsed=choice),side_effect=error)
    client=MagicMock();client.responses.parse=parse
    context=MagicMock();context.__aenter__=AsyncMock(return_value=client);context.__aexit__=AsyncMock(return_value=None)
    factory=MagicMock(return_value=context);monkeypatch.setattr(name_match,'AsyncOpenAI',factory)
    return parse


@pytest.mark.anyio
async def test_fast_luna_sees_only_caller_speech_and_names_and_returns_a_grounded_selection(monkeypatch):
    from app.config import Settings
    parse=model(monkeypatch,NameChoice(candidate=0,first_quote='Kasey',last_quote='Exammple'))
    turns=rows(('callee','I called as Kasey, uh, Exammple earlier.'),('assistant','Private assistant-only fact'))
    names=[dict(first_name='Casey',last_name='Example')]
    proof=await name_match.resolve_name(Settings(_env_file=None),turns,names)
    assert proof and proof.supports(turns,'Casey','Example')
    args=parse.await_args.kwargs
    assert args['model']=='gpt-6-luna' and args['service_tier']=='priority' and args['reasoning']=={'effort':'low'}
    assert args['store'] is False and 'tools' not in args and args['max_output_tokens']==384
    data=json.loads(args['input']);assert set(data)=={'caller_transcript','candidates'}
    assert 'assistant-only' not in args['input'] and data['candidates']==[dict(candidate=0,**names[0])]


@pytest.mark.anyio
@pytest.mark.parametrize('kind',['null','bad_index','invented_quote','assistant_quote','timeout'])
async def test_resolver_cannot_invent_a_candidate_or_caller_evidence(monkeypatch,kind):
    from app.config import Settings
    choice=NameChoice(candidate=None if kind=='null' else 7 if kind=='bad_index' else 0,
        first_quote='Casey',last_quote='Example')
    model(monkeypatch,choice,error=TimeoutError() if kind=='timeout' else None)
    turns=rows(('callee','Different Person'),('assistant','Casey Example'))
    assert await name_match.resolve_name(Settings(_env_file=None),turns,[dict(first_name='Casey',last_name='Example')]) is None


@pytest.mark.parametrize('text',[
    'Earlier I called as Kasey Exammple.',
    'First name is, uh, Kasey. My last name is Exammple.',
    '[clear throat ]Kasey. Exammple.',
    'Kasey Exammple. Actually, later I used a different name.',
])
def test_name_anywhere_is_enough_without_formal_identity_or_exact_spelling(text):
    turns=rows(('callee',text));proof=NameProof('Casey','Example','Kasey','Exammple')
    assert proof.supports(turns,'Casey','Example')
    assert not proof.supports(turns,'Other','Person')
    assert not proof.supports(rows(('assistant',text)),'Casey','Example')


@pytest.mark.parametrize('mode',['normal','cached','pending_audio','hangup','stale_google','sensitive'])
def test_native_relaxed_lookup_ignores_model_spelling_but_preserves_live_reads_and_no_write(service,tmp_path,mode):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    m.transition(cid,'completed');cid=m.db.create_inbound('CA_RELAXED_NAME','+12025550101',3,10,'concierge',True)
    m.transition(cid,'in_progress');m.settings.appointments_enabled=True
    speech(m,cid,'callee','Earlier, uh, Kasey',1000)
    speech(m,cid,'assistant','Please spell your first name,',2000);speech(m,cid,'callee','Okay',2500)
    speech(m,cid,'assistant','one letter at a time.',3000)
    speech(m,cid,'callee','[clear throat ]Exammple',4000)
    proof=NameProof('Casey','Example','Kasey','Exammple');resolver=AsyncMock(return_value=proof);g.name_resolver=resolver
    g.lookup_wait_seconds=.1;g.lookup_settle_seconds=.001
    if mode=='stale_google':p.events.clear()
    if mode=='sensitive':
        with g.store.connect() as db:db.execute("UPDATE private_appointments SET disclosure='sensitive'")
    if mode in ('pending_audio','hangup'):
        def interrupt():
            if mode=='pending_audio':m.caller_activity[cid]={'last_speech_ms':999999}
            else:m.transition(cid,'completed')
        p.read_hook=interrupt
    async def check():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_RELAXED_NAME');m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Misheard',last_name='Oldargument'))})
        if mode in ('normal','cached'):
            assert result['status']=='match' and result['location']=='Office A'
            if mode=='cached':
                again=await bridge.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Misheard',last_name='Oldargument'))})
                assert again['status']=='match' and resolver.await_count==1
        else:assert result['status']!='match' and not {'start','end','location','candidates'}&set(result)
        assert p.writes==1
        with g.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
            assert db.execute('SELECT count(*) FROM appointment_drafts WHERE call_id=?',(cid,)).fetchone()[0]==0
    client.portal.call(check)
