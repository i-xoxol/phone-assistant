import asyncio
import json
from datetime import timedelta
from app.calling.context import ExpectedCallUpdate,utcnow
from app.calling.knowledge import FactUpdate
from tests.test_calls import service
from tests.test_lookup import setup_call


def test_local_student_question_reads_approved_facts_without_luna(service):
    client,m,_=service;cid,bridge=setup_call(client,m)
    fact=FactUpdate(topic='Example course advising',text='For advising, bring your proposed course plan.',
        source='Owner approved course FAQ',expires_at=utcnow()+timedelta(days=2),approved_for_callers=True)
    m.knowledge.fact(fact)
    async def fail(*args): raise AssertionError('Local context should not invoke a model')
    m.lookup.runner=fail
    async def run():
        context=await bridge.tools.operation({'name':'get_call_context','call_id':'ctx',
            'arguments':json.dumps({'question':'student course advising'})})
        assert context['knowledge']['facts'][0]['text']==fact.text
        assert not m.lookup.list_for_call(cid)
    asyncio.run(run())


def test_relevant_and_exact_number_expected_notes_win_over_new_general_notes(service):
    client,m,_=service;cid,_=setup_call(client,m)
    expiry=utcnow()+timedelta(days=1)
    selected=m.context.expect(ExpectedCallUpdate(title='Student advising',context='Ask which course plan needs review.',expires_at=expiry))
    exact=m.context.expect(ExpectedCallUpdate(title='Anticipated callback',context='Ask whether the question was resolved.',
        phone_number='+12025550101',expires_at=expiry))
    for i in range(5):
        m.context.expect(ExpectedCallUpdate(title=f'General event {i}',context='Phone-safe event background.',expires_at=expiry))
    context=m.incoming_context(m.db.get(cid),question='student advising')
    ids=[note['context_id'] for note in context['expected_calls']]
    assert ids[0]==exact['context_id'] and selected['context_id'] in ids and len(ids)==3
    m.context.retire(selected['context_id'])
    assert selected['context_id'] not in [n['context_id'] for n in m.incoming_context(m.db.get(cid),question='student advising')['expected_calls']]
