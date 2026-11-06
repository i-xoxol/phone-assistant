"""Fictional per-contact guidance, scoped evidence, and owner controls."""
import json
from datetime import timedelta
from uuid import uuid4

import pytest

from app.api.personalities import Preview, prompt_preview
from app.calling.contact_guidance import ContactGuidance, GuidanceUpdate
from app.calling.context import utcnow
from app.calling.elevenlabs_session import realtime_config
from app.calling.gemini_session import gemini_config
from app.calling.inbound import startup_greeting_content
from app.calling.personalities import PersonalityUpdate
from app.calling.prompts import session_config
from app.storage.models import CallRequest
from tests.test_calls import service
from tests.test_phone_controls import contact, owner_headers


NUMBER = '+12025550101'


def entry(kind='instruction', title='Tone', text='Use a friendly tone.', **changes):
    return {'entry_id':str(uuid4()), 'kind':kind, 'title':title, 'text':text, **changes}


def save(manager, cid, entries):
    return manager.contact_guidance.save(cid, GuidanceUpdate(
        revision=manager.contact_guidance.get(cid)['revision'], entries=entries))


def profile(manager, name='Rocky example', mode='assistant', **changes):
    return manager.personalities.save(PersonalityUpdate(name=name, presentation_mode=mode, **changes))


def call_record(manager, personality, direction='outbound', number=NUMBER, forwarded=''):
    if direction == 'inbound':
        cid = manager.db.create_inbound('CA_fixture_' + str(uuid4()), number, 5, 100, 'concierge')
        manager.routing.save(cid, {'ForwardedFrom':forwarded})
    else:
        cid = manager.db.create(CallRequest(phone_number=number, objective='Fictional browser greeting check'))
    manager.personalities.pin(manager.db.get(cid), personality['personality_id'])
    return manager.voice_record(cid)


@pytest.mark.parametrize('direction', ['inbound','outbound'])
@pytest.mark.parametrize('scope', ['all','selected'])
@pytest.mark.parametrize('kind', ['instruction','fact'])
def test_all_four_combinations_in_each_call_direction(service, direction, scope, kind):
    _, manager, gateway = service
    cid = contact(manager, name='Riley Example')
    chosen = profile(manager)
    saved = save(manager, cid, [entry(kind, text='Fictional downstairs-neighbor guidance.',
        profile_scope=scope, profile_ids=[chosen['personality_id']] if scope == 'selected' else [])])
    record = call_record(manager, chosen, direction)
    guidance = record['contact_guidance']
    assert guidance['contact_id'] == str(cid) and guidance['revision'] == saved['revision']
    assert len(guidance['facts' if kind == 'fact' else 'instructions']) == 1
    assert 'Fictional downstairs-neighbor guidance.' in json.dumps(session_config(manager.settings, record))
    assert not gateway.calls


@pytest.mark.parametrize('direction', ['inbound','outbound'])
def test_most_specific_instruction_and_fact_win_without_other_profile_leaks(service, direction):
    _, manager, _ = service
    cid = contact(manager); chosen = profile(manager); other = profile(manager, 'Other profile')
    save(manager, cid, [
        entry(text='SHARED_TONE'),
        entry(text='DIRECTION_TONE', direction=direction),
        entry(text='PROFILE_TONE', profile_scope='selected', profile_ids=[chosen['personality_id']]),
        entry(text='FINAL_TONE', direction=direction, profile_scope='selected', profile_ids=[chosen['personality_id']]),
        entry('fact', 'Noise', 'SHARED_FACT'),
        entry('fact', ' noise ', 'PROFILE_FACT', profile_scope='selected', profile_ids=[chosen['personality_id']]),
        entry('fact', 'Restricted topic', 'OTHER_PROFILE_SECRET', profile_scope='selected', profile_ids=[other['personality_id']]),
        entry('instruction', 'Independent instruction', 'Keep answers brief.')])
    record = call_record(manager, chosen, direction)
    wire = json.dumps(session_config(manager.settings, record))
    assert 'FINAL_TONE' in wire and 'PROFILE_FACT' in wire and 'Keep answers brief.' in wire
    assert all(value not in wire for value in ('SHARED_TONE','DIRECTION_TONE','PROFILE_TONE','SHARED_FACT','OTHER_PROFILE_SECRET'))
    assert 'OTHER_PROFILE_SECRET' not in json.dumps(manager.knowledge.search('', call_id=record['call_id']))
    assert 'OTHER_PROFILE_SECRET' not in json.dumps(manager.incoming_context(record))


def test_multiple_selected_profiles_and_future_all_profile_inheritance(service):
    _, manager, _ = service
    cid = contact(manager); one = profile(manager); two = profile(manager, 'Second')
    save(manager, cid, [entry('fact','Shared','ALL_PROFILES'),entry('fact','Selected','SELECTED_ONLY',
        profile_scope='selected', profile_ids=[one['personality_id'],two['personality_id']])])
    future = profile(manager, 'Created after guidance')
    for p in (one,two,future):
        record = call_record(manager,p)
        texts = [e['text'] for e in record['contact_guidance']['facts']]
        assert 'ALL_PROFILES' in texts
        assert ('SELECTED_ONLY' in texts) == (p != future)


@pytest.mark.parametrize('number,forwarded,duplicate', [('', '', False),('+12025550102','',False),
    (NUMBER,NUMBER,False),(NUMBER,'',True)])
def test_unknown_withheld_ambiguous_or_duplicate_identity_gets_no_guidance(service, number, forwarded, duplicate):
    _, manager, _ = service
    cid=contact(manager); chosen=profile(manager)
    save(manager,cid,[entry('fact',text='CONTACT_ONLY_SECRET')])
    if duplicate: contact(manager, name='Duplicate Example')
    record=call_record(manager,chosen,'inbound',number,forwarded)
    assert not record['contact_guidance']['contact_id']
    assert 'CONTACT_ONLY_SECRET' not in json.dumps(session_config(manager.settings,record))


@pytest.mark.parametrize('provider', ['openai','elevenlabs','gemini'])
@pytest.mark.parametrize('mode', ['assistant','owner_first_person'])
@pytest.mark.parametrize('direction', ['inbound','outbound'])
def test_greetings_and_all_provider_planners_share_contact_preferences(service, provider, mode, direction):
    _, manager, _ = service
    manager.settings.live_tools_enabled = True
    cid=contact(manager,name='Riley Example')
    chosen=profile(manager, mode=mode, spoken_name='Rocky' if mode=='assistant' else 'Alex')
    greeting="Hi Riley, it's "+('Rocky.' if mode=='assistant' else 'Alex.')
    save(manager,cid,[entry(text='Be friendly and lightly playful.',greeting=greeting)])
    record=call_record(manager,chosen,direction)
    config=session_config(manager.settings,record)
    if provider=='elevenlabs':
        wire=realtime_config(manager.settings,record,{'model':'eleven_realtime_v1_max','voice':'test-clone'})['instructions']
    elif provider=='gemini':
        wire=gemini_config(manager.settings,record,{'model':'gemini-3.8-live','voice':'Kore'})['system_instruction']
    else: wire=config['instructions']
    assert greeting in wire and 'friendly and lightly playful' in wire
    assert 'friendly and lightly playful' in config['delegation']['responses']['instructions']
    if direction=='inbound': assert startup_greeting_content(manager.settings,record)==greeting
    else:
        from app.calling.presentation import outbound_opening
        assert greeting in outbound_opening(manager.settings,record)
    record['transcript']=[{'speaker':'callee','text':'Continue the request.','timestamp':0,'end_timestamp':1}]
    assert startup_greeting_content(manager.settings,record) is None


@pytest.mark.parametrize('change', ['edit','disable','expire','remove'])
def test_facts_withdraw_on_later_reads_but_instructions_are_pinned(service, change):
    _, manager, _ = service
    cid=contact(manager); chosen=profile(manager)
    instruction=entry(text='PINNED_STYLE'); fact=entry('fact','Noise','ORIGINAL_FACT')
    save(manager,cid,[instruction,fact]); record=call_record(manager,chosen)
    before=manager.knowledge.search('noise',call_id=record['call_id'])['facts']
    assert len(before)==1 and before[0]['expires_at'] is None
    instruction['text']='NEW_STYLE'
    if change=='edit':fact['text']='CHANGED_FACT'
    elif change=='disable':fact['enabled']=False
    elif change=='expire':fact['expires_at']=(utcnow()-timedelta(seconds=1)).isoformat()
    save(manager,cid,[instruction]+([] if change=='remove' else [fact]))
    current=manager.voice_record(record['call_id'])
    assert current['contact_guidance']['instructions'][0]['text']=='PINNED_STYLE'
    assert current['contact_guidance']['facts']==[]
    assert manager.knowledge.still_current(before,call_id=record['call_id'])==[]
    assert manager.knowledge.search('noise',call_id=record['call_id'])['facts']==[]
    fresh=call_record(manager,chosen)
    assert fresh['contact_guidance']['instructions'][0]['text']=='NEW_STYLE'


def test_expired_fact_filtered_without_owner_edit_and_other_contacts_cannot_query_it(service):
    _, manager, _ = service
    cid=contact(manager); chosen=profile(manager)
    saved=save(manager,cid,[entry('fact','Noise','VISIBLE_STABLE'),entry('fact','Old','EXPIRED_SECRET',
        expires_at=(utcnow()-timedelta(seconds=1)).isoformat())])
    record=call_record(manager,chosen)
    assert [e['text'] for e in record['contact_guidance']['facts']]==['VISIBLE_STABLE']
    other=call_record(manager,chosen,number='+12025550102')
    assert manager.knowledge.search('noise',caller_number=NUMBER,call_id=other['call_id'])['facts']==[]
    assert manager.knowledge.search('noise',caller_number=NUMBER)['facts']==[]
    assert manager.knowledge.search(owner=True)['facts']==[]
    assert manager.contact_guidance.get(cid)['revision']==saved['revision']


def test_profile_facts_do_not_leak_through_call_history(service):
    _,manager,_=service
    cid=contact(manager); one=profile(manager); two=profile(manager,'Other')
    save(manager,cid,[entry('fact','Private background','PROFILE_ONLY_BACKGROUND',profile_scope='selected',profile_ids=[one['personality_id']])])
    first=call_record(manager,one)
    manager.db.transcript(first['call_id'],{'type':'session.output_transcript.delta','event_id':'fictional-fact',
        'start_ms':0,'end_ms':1000,'delta':'PROFILE_ONLY_BACKGROUND'})
    manager.transition(first['call_id'],'completed')
    next_call=call_record(manager,two)
    history=manager.memory.records(NUMBER,next_call['call_id'])
    assert history and 'PROFILE_ONLY_BACKGROUND' not in json.dumps(history)
    assert history[0]['conversation']==[]


def test_owner_api_revision_validation_and_entry_contact_binding(service):
    client,manager,_=service
    cid=contact(manager); path='/live/contacts/'+str(cid)+'/guidance'; headers=owner_headers(client)
    assert client.get(path).status_code==401
    assert client.put(path,headers={**headers,'X-CSRF-Token':'bad'},json={'revision':0,'entries':[]}).status_code==403
    assert client.get(path,headers=headers).json()['revision']==0
    request={'revision':0,'entries':[entry()]}
    result=client.put(path,headers=headers,json=request)
    assert result.status_code==200 and result.json()['revision']==1
    assert client.put(path,headers=headers,json=request).status_code==409
    assert client.get('/concierge/contacts/'+str(cid)+'/guidance').json()['entries']==result.json()['entries']
    other=contact(manager,number='+12025550102')
    assert client.put('/concierge/contacts/'+str(other)+'/guidance',json=request).status_code==409
    for invalid in [entry(profile_scope='selected'),entry(profile_ids=[str(uuid4())]),
        entry(profile_scope='selected',profile_ids=[str(uuid4())]),entry('fact',greeting='Invalid')]:
        response=client.put(path,headers=headers,json={'revision':1,'entries':[invalid]})
        assert response.status_code in (409,422)
    assert manager.contact_guidance.get(cid)['revision']==1


def test_preview_shows_effective_name_direction_and_scopes_without_writes(service):
    _,manager,gateway=service
    cid=contact(manager); chosen=profile(manager,spoken_name='Rocky')
    save(manager,cid,[entry(direction='outbound',greeting="Hi Riley, it's Rocky."),entry('fact','Noise','FICTIONAL_CAT')])
    with manager.db.connect() as db:
        before=[db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('calls','call_personalities','call_contact_guidance')]
    preview=prompt_preview(manager,Preview(contact_id=cid,personality_id=chosen['personality_id'],direction='outbound'))
    assert preview['presentation']['spoken_name']=='Rocky'
    assert preview['contact_guidance']['greeting']=="Hi Riley, it's Rocky."
    assert 'FICTIONAL_CAT' in preview['voice_instructions']
    ambiguous=prompt_preview(manager,Preview(contact_id=cid,forwarded_from=NUMBER))
    assert ambiguous['contact_guidance']['contact_id'] is None
    with manager.db.connect() as db:
        after=[db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('calls','call_personalities','call_contact_guidance')]
    assert before==after and not gateway.calls


def test_migration_does_not_apply_new_guidance_to_historical_calls(service):
    _,manager,_=service
    cid=contact(manager); chosen=profile(manager); record=call_record(manager,chosen)
    save(manager,cid,[entry('fact',text='NEW_CONTACT_FACT')])
    with manager.db.connect() as db:
        db.execute('DELETE FROM call_contact_guidance');db.execute('DELETE FROM contact_guidance_migration')
    manager.contact_guidance=ContactGuidance(manager)
    assert manager.contact_guidance.for_record(record)['facts']==[]
    assert ContactGuidance(manager).get(cid)['entries']


def test_active_fact_expiry_is_rechecked_without_an_edit(service, monkeypatch):
    _,manager,_=service;cid=contact(manager);chosen=profile(manager)
    stamp=utcnow()
    save(manager,cid,[entry('fact','Temporary','EXPIRES_LATER',expires_at=(stamp+timedelta(minutes=1)).isoformat())])
    record=call_record(manager,chosen)
    previous=manager.knowledge.search('',call_id=record['call_id'])['facts']
    assert previous
    monkeypatch.setattr('app.calling.contact_guidance.utcnow',lambda:stamp+timedelta(minutes=2))
    assert manager.knowledge.still_current(previous,call_id=record['call_id'])==[]
    assert manager.voice_record(record['call_id'])['contact_guidance']['facts']==[]


def test_calendar_voice_inherits_contact_style_without_unrelated_facts_or_greetings(service):
    from app.calling.elevenlabs_session import calendar_voice_policy,CALENDAR_SPEECH_POLICY
    _,manager,_=service;cid=contact(manager);chosen=profile(manager)
    save(manager,cid,[entry(text='Use a quietly playful style.',greeting='UNUSED_OPENING'),
        entry('fact','Private unrelated fact','UNRELATED_FACT')])
    record=call_record(manager,chosen)
    text=calendar_voice_policy(manager.settings,record)
    assert 'quietly playful' in text and text.endswith(CALENDAR_SPEECH_POLICY)
    assert 'UNUSED_OPENING' not in text and 'UNRELATED_FACT' not in text


def test_long_fact_and_overlapping_titles_cannot_save(service):
    from pydantic import ValidationError
    _,manager,_=service;cid=contact(manager)
    with pytest.raises(ValidationError,match='250 UTF-8'):
        save(manager,cid,[entry('fact',text='я'*126)])
    with pytest.raises(ValidationError,match='distinct scopes'):
        save(manager,cid,[entry(),entry()])
    assert manager.contact_guidance.get(cid)['revision']==0
