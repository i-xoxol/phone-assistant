import asyncio
import json
from datetime import timedelta,datetime
from uuid import uuid4
import xml.etree.ElementTree as ET

import pytest

from app.calling.context import utcnow
from app.calling.lookup import Selection
from app.calling.prompts import session_config
from app.storage.models import CallRequest
from app.mcp.server import create_mcp
from tests.test_calls import service
from tests.test_inbound import incoming,SID
from tests.test_lookup import setup_call

NUMBER='+12025550101'


def previous(m,number=NUMBER,status='no_answer',objective='Ask whether the demo bicycle is ready.'):
    cid=m.db.create(CallRequest(phone_number=number,objective=objective))
    m.transition(cid,status)
    return cid


def caption(m,cid,speaker,text,event=None,start=100):
    m.db.transcript(cid,{'type':'session.input_transcript.delta' if speaker=='callee' else 'session.output_transcript.delta',
        'event_id':event or str(uuid4()),'delta':text,'start_ms':start,'end_ms':start+100})


def test_exact_number_history_in_both_directions_excludes_current_and_other_numbers(service):
    client,m,g=service;m.settings.call_history_enabled=True
    outbound=previous(m)
    inbound=m.db.create_inbound('CA'+'9'*32,NUMBER,5,10,'concierge')
    m.transition(inbound,'completed');caption(m,inbound,'callee','The demo bicycle will be ready Tuesday.')
    other=previous(m,number='+12025550108',objective='Unrelated private task.')
    cid,bridge=setup_call(client,m)
    context=m.voice_record(cid)['incoming_context']['call_history']
    assert context['match_count']==2
    assert {r['call_id'] for r in context['recent_calls']}=={outbound,inbound}
    evidence=m.memory.candidates(m.db.get(cid))
    assert 'bicycle' in json.dumps(evidence) and 'Tuesday' in json.dumps(evidence)
    assert NUMBER not in json.dumps(evidence) and 'Unrelated' not in json.dumps(evidence)
    assert other not in json.dumps(evidence) and cid not in json.dumps(evidence)


def test_unknown_withheld_old_and_future_calls_do_not_enter_recall(service):
    client,m,g=service;m.settings.call_history_enabled=True
    old=previous(m)
    with m.db.connect() as db:
        db.execute('UPDATE calls SET created_at=? WHERE call_id=?',((utcnow()-timedelta(days=100)).isoformat(),old))
    cid,bridge=setup_call(client,m)
    future=previous(m)
    assert not m.memory.candidates(m.db.get(cid))
    assert m.memory.records('')==[] and m.memory.records('anonymous')==[]
    assert not m.memory.records('+12025550109')


def test_sensitive_question_answer_numbers_and_urls_redacted_before_luna(service):
    client,m,g=service;m.settings.call_history_enabled=True
    old=previous(m,status='completed')
    caption(m,old,'callee','What is the date of birth?',start=100)
    caption(m,old,'assistant','July third, twenty twenty-one.',start=300)
    caption(m,old,'callee','Use 123-45-6789 and secret@example.test at https://example.test/private.',start=500)
    cid,bridge=setup_call(client,m)
    serialized=json.dumps(m.memory.candidates(m.db.get(cid)))
    for secret in ('July third','123-45-6789','secret@example.test','https://example.test/private'):
        assert secret not in serialized
    assert 'Original request:' in serialized


def test_unanswered_callback_automatic_luna_context_is_internal_and_idempotent(service):
    client,m,g=service;m.settings.call_history_enabled=True
    old=previous(m)
    async def exercise():
        cid,bridge=setup_call(client,m)
        async def choose(question,facts,timeout):
            selected=next(f for f in facts if f.get('source_call_id')==old)
            assert selected['source_status']=='no_answer'
            return Selection(selected_ids=[selected['fact_id']],needs_supervisor=False)
        m.lookup.runner=choose
        await m.lookup.prefetch_history(cid)
        await asyncio.gather(*list(m.lookup.tasks.values()))
        await m.lookup.prefetch_history(cid)
        result=m.result(cid)
        assert len(result['lookup_requests'])==1
        update=result['call_updates'][0]
        assert update['mode']=='context' and 'Internal historical recall' in update['content']
        assert 'bicycle' in update['content'] and 'no_answer' in update['content']
        instructions=session_config(m.settings,m.voice_record(cid))['instructions']
        assert 'Caller ID can be spoofed' in instructions and 'does not mean a conversation took place' in instructions
    asyncio.run(exercise())


def test_followup_message_and_updated_previous_context_are_rechecked(service):
    client,m,g=service;m.settings.call_history_enabled=True
    old=m.db.create_inbound('CA'+'9'*32,NUMBER,5,10,'concierge')
    m.transition(old,'completed')
    m.db.save_message(old,'The demo bicycle is ready Tuesday.',caller_name='Casey')
    cid,bridge=setup_call(client,m)
    record=m.db.get(cid)
    candidates=m.memory.candidates(record)
    assert any('confirmed message' in f['text'] for f in candidates)
    with m.db.connect() as db:
        db.execute('UPDATE calls SET objective=? WHERE call_id=?',('Changed request.',old))
    assert len(m.memory.still_current(record,candidates))<len(candidates)


def test_message_only_can_recall_but_cannot_gain_supervisor_or_booking_tools(service):
    client,m,g=service;m.settings.call_history_enabled=True;m.settings.subscription_lookup_enabled=True
    previous(m)
    incoming(client,m)
    cid=m.db.by_twilio_sid(SID)['call_id']
    config=session_config(m.settings,m.voice_record(cid))
    assert {t['name'] for t in config['delegation']['responses']['tools']}=={'save_message','end_call','request_quick_lookup'}
    assert 'message-taking only' in config['instructions']
    assert 'outbound/keypad, lookup, payment' not in config['delegation']['responses']['instructions']


def test_owner_rest_and_mcp_history_are_authenticated_and_read_only(service):
    client,m,g=service
    old=previous(m)
    assert client.get('/calls/history',params={'phone_number':NUMBER},headers={'Authorization':''}).status_code==401
    assert client.get('/calls/history',params={'phone_number':'555'}).status_code==422
    assert client.get('/calls/history',params={'phone_number':NUMBER}).json()['calls'][0]['call_id']==old
    m.settings.oauth_database_path=m.db.path.parent/'oauth.sqlite3'
    server,_=create_mcp(m)
    async def exercise():
        result=await server.call_tool('get_contact_call_history',{'phone_number':NUMBER})
        assert not result.is_error
        assert json.loads(result.content[0].text)['calls'][0]['call_id']==old
    asyncio.run(exercise())
    assert not g.calls


@pytest.mark.parametrize('closed,behavior',[(False,'closed'),(True,'closed'),(True,'messages')])
def test_delay_is_first_twiml_verb_before_answering_for_all_incoming_modes(service,closed,behavior):
    from app.calling.hours import HoursSettings,HoursWindow
    client,m,g=service;m.settings.inbound_answer_delay_seconds=18
    if closed:
        m.hours.set(HoursSettings(enabled=True,office_hours=[],outside_hours=behavior))
    xml=ET.fromstring(incoming(client,m).text)
    assert xml[0].tag=='Pause' and xml[0].attrib=={'length':'18'}
    assert xml[1].tag==('Say' if closed else 'Connect')


def test_zero_delay_and_outbound_or_resumed_voice_are_unchanged(service):
    from app.calling.twilio_client import stream_twiml
    client,m,g=service
    assert ET.fromstring(incoming(client,m).text)[0].tag=='Connect'
    m.settings.inbound_answer_delay_seconds=18
    assert ET.fromstring(stream_twiml(m.settings,'example'))[0].tag=='Connect'
