"""A real caller supplied both spellings; intake must not restart afterward."""
import json
from datetime import datetime,timezone
import pytest
from app.appointments.evidence import caller_identified
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,agree,speech


def repaired(first='Sure. Uh, Casey, C A S E Y',last='Exammple. E X A M P L E',repeat=False):
    pairs=[('assistant','Can I have your first and last name?'),('callee','My My name- Yes, um, Casey Exammple'),
        ('assistant','Please hold while I check a different spelling. Could you spell your first name?'),
        ('callee',first),('assistant','Thank you. Now the last name, please.'),('callee',last)]
    if repeat:pairs.append(('assistant','I need to confirm your name again. Could you please repeat your full name?'))
    return [dict(speaker=s,text=t,timestamp=i,end_timestamp=i+.5) for i,(s,t) in enumerate(pairs)]


@pytest.mark.parametrize('first,last',[
    ('Sure. Uh, Casey, C A S E Y','Exammple. E X A M P L E'),
    ('Casey. C-A-S-E-Y','Example, E-X-A-M-P-L-E'),
    ('Yeah. C. A. S. E. Y.','Oh, sure. Example. E X A M P L E'),
    ('Yes. Um, Kasey, C A S E Y','Example, E X A M P L E'),
    (' Sure. Uh, Casey, C A S E Y',' Exammple. E X A M P L E'),
    ('Jordan C A S E Y','Exemplar. E X A M P L E'),
    ('C A S E Y. Casey','E X A M P L E. Exemplar'),
])
def test_name_labels_plus_exact_letters_and_bare_last_name_prompt_complete_one_lookup(first,last):
    rows=repaired(first,last)
    assert caller_identified(rows,'Casey','Example',for_lookup=True)
    assert not caller_identified(rows,'Casey','Example')


def test_false_start_self_introduction_answers_an_explicit_full_name_question():
    assert caller_identified(repaired()[:2],'Casey','Exammple',for_lookup=True)


def test_an_assistant_repeating_the_question_does_not_revoke_complete_caller_evidence():
    assert caller_identified(repaired(repeat=True),'Casey','Example',for_lookup=True)
    rows=repaired(repeat=True)+[dict(speaker='callee',text="Actually, I'm",timestamp=8,end_timestamp=8.5)]
    assert not caller_identified(rows,'Casey','Example',for_lookup=True)


@pytest.mark.parametrize('first,last',[
    ('Casey C A S','Example E X A M P L E'),('Casey K A S E Y','Example E X A M P L E'),
    ('for C A S E Y','Example E X A M P L E'),
    ('Casey C A S E Y for my friend','Example E X A M P L E'),('Casey C A S E Y','not E X A M P L E'),
    ('Casey C A S E Y but tomorrow','Example E X A M P L E'),('Casey C A S E Y','Example E X A M P L'),
])
def test_clarification_never_drops_partial_conflicting_conditional_or_third_party_content(first,last):
    assert not caller_identified(repaired(first,last),'Casey','Example',for_lookup=True)


def test_a_bare_last_name_question_cannot_borrow_an_uncollected_first_name():
    assert not caller_identified(repaired()[4:],'Casey','Example',for_lookup=True)


@pytest.mark.parametrize('first',['Yes','Sure'])
def test_name_card_does_not_turn_a_filler_named_person_into_a_different_first_name(first):
    from app.appointments.evidence import lookup_name_card
    rows=[dict(speaker='assistant',text='Your full name?',timestamp=0,end_timestamp=.5),
        dict(speaker='callee',text=first+' Example',timestamp=1,end_timestamp=1.5)]
    assert caller_identified(rows,first,'Example',for_lookup=True)
    assert lookup_name_card(rows)==dict(first_name=None,last_name=None)


@pytest.mark.parametrize('field',['first','last'])
def test_one_component_answer_cannot_be_reinterpreted_as_two_name_fields(field):
    rows=[dict(speaker='assistant',text='Your '+field+' name?',timestamp=0,end_timestamp=.5),
        dict(speaker='callee',text='Casey Example',timestamp=1,end_timestamp=1.5)]
    assert not caller_identified(rows,'Casey','Example',for_lookup=True)


def test_a_current_first_name_is_reused_when_only_the_surname_needs_spelling(service,tmp_path):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    m.transition(cid,'completed');cid=m.db.create_inbound('CA_LAST_ONLY','+12025550101',3,10,'concierge',True)
    m.transition(cid,'in_progress');m.settings.appointments_enabled=True
    speech(m,cid,'callee','My name is Casey Exammple.',1000)
    async def check():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_LAST_ONLY');m.attach_bridge(cid,bridge)
        args=json.dumps(dict(first_name='Casey',last_name='Exammple'))
        result=await bridge.tools.operation({'name':'search_appointment_name','arguments':args})
        assert result['status']=='needs_name_spelling' and result['missing_fields']==['last_name']
        assert 'spell your last name' in result['instruction'] and 'spell your first name' not in result['instruction']
        speech(m,cid,'assistant','Could you spell your last name?',2000)
        speech(m,cid,'callee',' Exammple. E X A M P L E',3000)
        result=await bridge.tools.operation({'name':'search_appointment_name','arguments':args})
        assert result['status']=='match' and result['location']=='Office A' and p.writes==1
    client.portal.call(check)


@pytest.mark.parametrize('kind',['valid','repeat_full','repeat_spelling','status_only','component_intake'])
def test_simulator_rejects_name_loops_even_when_the_appointment_is_eventually_verified(kind):
    from scripts.simulate_appointment_lookup import evaluate
    pairs=[('assistant','Can I have your first and last name?'),('callee','My name is Casey Exammple.'),
        ('assistant','Could you spell your last name?'),('callee','Example, E X A M P L E')]
    if kind=='component_intake':pairs=[('assistant','Your first name?'),('callee','Kasey'),('assistant','Your last name?'),('callee','Exammple'),
        ('assistant','Could you spell your first name?'),('callee','C A S E Y'),('assistant','Could you spell your last name?'),('callee','E X A M P L E')]
    if kind=='repeat_full':pairs.append(('assistant','Could you repeat your full name?'))
    if kind=='repeat_spelling':pairs.extend([('assistant','Could you spell your last name?'),('callee','Example, E X A M P L E')])
    if kind=='status_only':pairs.append(('assistant','Thanks, I have your name. Let me check.'))
    events=[dict(side='assistant',direction='input' if s=='callee' else 'output',text=t,start_ms=i*1000,end_ms=i*1000+500) for i,(s,t) in enumerate(pairs)]
    details=dict(status='match',start='2026-10-26T15:15:00-04:00',end='2026-10-26T15:30:00-04:00',zone='America/New_York',location='Office A')
    report=dict(started_at='2026-10-25T12:00:00+00:00',caller_name=dict(first_name='Casey',last_name='Example'),transcript=events,
        tools=[dict(result=details,transcript_index=len(events))],blocked_tools=[])
    heard='October 26th at 3:15 p.m. Office A.'
    events.extend([dict(side='assistant',direction='output',text=heard),dict(side='caller',direction='input',text=heard)])
    result=evaluate(report)
    assert result['passed']==(kind in ('valid','status_only','component_intake'))
    if kind not in ('valid','status_only','component_intake'):assert result['reason']=='name_clarification_loop'


@pytest.mark.parametrize('mode',['complete','stale_both','missing_last','exhausted','superseded','first_exhausted','last_exhausted','unknown_first'])
def test_native_repair_matches_or_reports_only_the_missing_field_without_another_name_loop(service,tmp_path,mode):
    client,m,p,g,cid,checked=prepare(service,tmp_path);agree(m,g,cid,checked)
    client.portal.call(g.book,cid,dict(draft_id=checked['draft_id'],check_id=checked['check_id'],idempotency_key='one'))
    m.transition(cid,'completed');cid=m.db.create_inbound('CA_NAME_REPAIR','+12025550101',3,10,'concierge',True)
    m.transition(cid,'in_progress');m.settings.appointments_enabled=True
    rows=repaired() if mode!='exhausted' else repaired('Not my name','A different person')
    if mode=='missing_last':rows=rows[:-1]
    if mode=='superseded':rows.append(dict(speaker='callee',text="Actually, I'm Jordan",timestamp=9,end_timestamp=9.5))
    if mode=='first_exhausted':rows=repaired('Not my name')[:4]
    if mode=='last_exhausted':rows=[dict(speaker=s,text=t) for s,t in [('callee','My name is Casey Exammple.'),('assistant','Could you spell your last name?'),('callee','Not my name')]]
    if mode=='unknown_first':rows=[dict(speaker=s,text=t) for s,t in [('callee','My name is Kasey Exammple.'),('assistant','Could you spell your last name?'),('callee','Exammple, E X A M M P L E')]]
    for i,row in enumerate(rows):speech(m,cid,row['speaker'],row['text'],1000+i*1000)
    async def check():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_NAME_REPAIR');m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation({'name':'search_appointment_name','arguments':json.dumps(dict(first_name='Obsolete' if mode=='stale_both' else 'Casey',last_name='Oldlabel' if mode=='stale_both' else 'Exammple' if mode=='complete' else 'Example'))})
        if mode in ('complete','stale_both'):assert result['status']=='match' and result['location']=='Office A'
        elif mode=='missing_last':assert result['status']=='needs_name_spelling' and result['missing_fields']==['last_name']
        elif mode=='unknown_first':assert result['status']=='not_found' and result['reason']=='exact_name_not_found_after_spelling'
        else:assert result['reason']=='name_clarification_exhausted' and 'Do not ask' in result['instruction']
        if mode not in ('complete','stale_both'):assert not {'start','end','location','candidates'}&set(result)
        assert p.writes==1
        with g.store.connect() as db:assert db.execute('SELECT count(*) FROM booking_operations WHERE call_id=?',(cid,)).fetchone()[0]==0
    client.portal.call(check)
