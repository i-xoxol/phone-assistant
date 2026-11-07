"""Developing fiction is durable, isolated and independent of real action grants."""
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.calling.character_biography import (
    BiographyUpdate,CharacterBiographies,RememberPersonalFacts,canonical_key,
)
from app.calling.personalities import PersonalityUpdate
from app.calling.prompts import session_config
from app.calling.elevenlabs_session import realtime_config
from app.calling.gemini_session import gemini_config
from app.calling.tools import VoiceTools
from tests.test_calls import service
from tests.test_character_personalities import character
from tests.test_presentation import record_for
from tests.test_phone_controls import owner_headers


def developing(m,name='Jordan Example'):
    return m.personalities.save(PersonalityUpdate(name=name,presentation_mode='character',allow_character_invention=True))


def active(m,profile=None,direction='outbound'):
    record=record_for(m,direction,profile or developing(m))
    m.transition(record['call_id'],'in_progress')
    return m.voice_record(record['call_id'])


def remember(m,record,key='favorite_breakfast',value='Pancakes',tool='memory-one'):
    return m.character_biographies.remember(record['call_id'],tool,RememberPersonalFacts(facts=[{'key':key,'value':value}]))


def replace(m,pid,revision=0,**facts):
    return m.character_biographies.replace(pid,BiographyUpdate(revision=revision,facts=[{'key':k,'value':v} for k,v in facts.items()]))


def test_invention_is_explicit_character_only_and_legacy_profiles_stay_off(service):
    _,m,_=service
    assert all(not p['allow_character_invention'] for p in m.personalities.catalog()['personalities'])
    for mode in ('assistant','owner_first_person'):
        with pytest.raises(ValueError):m.personalities.save(PersonalityUpdate(name='Denied',presentation_mode=mode,allow_character_invention=True))
    profile=developing(m);pid=profile['personality_id']
    saved=m.personalities.save(PersonalityUpdate(name='Edited',revision=1),pid)
    assert saved['allow_character_invention']
    saved=m.personalities.save(PersonalityUpdate(name='Assistant now',revision=2,presentation_mode='assistant'),pid)
    assert not saved['allow_character_invention']


def test_persists_reuses_owner_canon_and_aliases_without_overwriting(service):
    _,m,_=service;record=active(m);pid=record['personality']['personality_id']
    replace(m,pid,date_of_birth='1981-02-03',spouse_name='Casey')
    result=remember(m,record,'Wife name','Taylor')
    assert result['conflicting_keys']==['spouse_name'] and not result['accepted_keys']
    assert remember(m,record,'birthday','1982-01-01',tool='memory-two')['conflicting_keys']==['date_of_birth']
    result=remember(m,record,tool='memory-three')
    assert result['accepted_keys']==['favorite_breakfast']
    again=CharacterBiographies(m).get(pid)
    assert {f['key']:f['value'] for f in again['facts']}=={'date_of_birth':'1981-02-03','spouse_name':'Casey','favorite_breakfast':'Pancakes'}
    second=active(m,m.personalities.get(pid))
    assert m.character_biographies.read_call(second['call_id'])['facts']==again['facts']
    assert 'Pancakes' in session_config(m.settings,second)['instructions']
    assert remember(m,second,value='Waffles')['conflicting_keys']==['favorite_breakfast']


def test_idempotency_and_retired_topics_never_restore_withdrawn_values(service):
    _,m,_=service;record=active(m);pid=record['personality']['personality_id']
    remember(m,record)
    assert remember(m,record,'hobby','Cycling')['status']=='already_saved'
    assert 'hobby' not in str(m.character_biographies.get(pid))
    replace(m,pid,revision=1)
    result=remember(m,record,tool='second')
    assert result['conflicting_keys']==['favorite_breakfast']
    assert result['biography']['retired_keys']==['favorite_breakfast']
    assert 'Pancakes' not in json.dumps(result)
    assert 'Pancakes' not in json.dumps(remember(m,record))
    with pytest.raises(ValueError,match='changed elsewhere'):replace(m,pid,revision=1,hobby='Cycling')
    assert m.character_biographies.get(pid)['entries'][0]['enabled'] is False


def test_simultaneous_calls_first_write_wins_and_other_characters_are_independent(service):
    _,m,_=service;profile=developing(m);one=active(m,profile);two=active(m,profile)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda pair:remember(m,pair[0],value=pair[1]),[(one,'Pancakes'),(two,'Waffles')]))
    assert sum(bool(r['accepted_keys']) for r in results)==1
    canon=m.character_biographies.get(profile['personality_id'])['facts']
    assert all(r['biography']['facts']==canon for r in results)
    other=active(m,developing(m,'Another character'))
    assert remember(m,other,value='Toast')['accepted_keys']
    assert m.character_biographies.get(profile['personality_id'])['facts']==canon


def test_duplicate_clones_an_independent_biography(service):
    _,m,_=service;profile=developing(m);pid=profile['personality_id']
    replace(m,pid,hobby='Bicycle repair')
    clone=m.personalities.duplicate(pid,'Independent copy');cpid=clone['personality_id']
    assert clone['allow_character_invention']
    assert m.character_biographies.get(cpid)['facts']==m.character_biographies.get(pid)['facts']
    replace(m,cpid,revision=1,hobby='Gardening')
    assert m.character_biographies.get(pid)['facts'][0]['value']=='Bicycle repair'


@pytest.mark.parametrize('state',['ended','disabled','mode_changed','assistant','me'])
def test_memory_writes_require_active_pinned_and_current_character_authority(service,state):
    _,m,_=service
    profile=(m.personalities.save(PersonalityUpdate(name='Normal identity',presentation_mode='assistant' if state=='assistant' else 'owner_first_person'))
             if state in ('assistant','me') else developing(m))
    record=active(m,profile);pid=profile['personality_id']
    if state=='ended':m.transition(record['call_id'],'completed')
    elif state=='disabled':m.personalities.save(PersonalityUpdate(name=profile['name'],revision=1,allow_character_invention=False),pid)
    elif state=='mode_changed':
        m.personalities.save(PersonalityUpdate(name=profile['name'],revision=1,presentation_mode='assistant'),pid)
        assert m.voice_record(record['call_id'])['personality']['presentation_mode']=='character'
        assert 'New invention is disabled' in session_config(m.settings,m.voice_record(record['call_id']))['instructions']
    with pytest.raises(ValueError):remember(m,record)


@pytest.mark.parametrize('state',['current','closing','handoff','superseded','stopping'])
def test_tool_is_bound_to_current_bridge_and_rejects_profile_spoofing(service,state):
    _,m,_=service;record=active(m);cid=record['call_id']
    bridge=SimpleNamespace(manager=m,call_id=cid,closing=False,handoff=False)
    m.bridges[cid]=bridge
    if state=='superseded':m.bridges[cid]=object()
    elif state=='stopping':m.stopping.add(cid)
    elif state!='current':setattr(bridge,state,True)
    tool=VoiceTools(bridge)
    item={'name':'remember_character_facts','call_id':'one','arguments':json.dumps({'facts':[{'key':'hobby','value':'Gardening'}]})}
    try:
        if state=='current':
            assert asyncio.run(tool.operation(item))['accepted_keys']==['hobby']
            spoof={**item,'arguments':json.dumps({'personality_id':'another','facts':[{'key':'hobby','value':'Other'}]})}
            with pytest.raises(ValidationError):asyncio.run(tool.operation(spoof))
        else:
            with pytest.raises(ValueError):asyncio.run(tool.operation(item))
    finally:m.bridges.pop(cid,None);m.stopping.discard(cid)


@pytest.mark.parametrize('direction',['inbound','outbound'])
@pytest.mark.parametrize('provider',['openai','elevenlabs','gemini'])
def test_every_provider_uses_saved_canon_and_only_adds_character_tools(service,direction,provider):
    _,m,_=service;profile=developing(m);replace(m,profile['personality_id'],hobby='Gardening')
    record=active(m,profile,direction);base={**record,'personality':{**profile,'allow_character_invention':False}}
    config=session_config(m.settings,record);ordinary=session_config(m.settings,base)
    names=lambda c:{t['name'] for t in c.get('delegation',{}).get('responses',{}).get('tools',[])}
    assert names(config)-names(ordinary)=={'get_character_biography','remember_character_facts'}
    if provider=='openai':text=config['instructions']
    elif provider=='elevenlabs':text=realtime_config(m.settings,record,{'model':'eleven_realtime_v1_max','voice':'example'})['instructions']
    else:text=gemini_config(m.settings,record,{'model':'gemini-3.8-live','voice':'Kore'})['system_instruction']
    assert 'Gardening' in text and 'BEFORE saying it' in text and 'do not ask the owner or supervisor' in text
    assert 'honestly' in text and 'No further owner permission is needed' in text
    assert 'New invention is disabled' in ordinary['instructions']


def test_outbound_memory_only_delegation_does_not_enable_real_world_tools(service):
    _,m,_=service;m.settings.live_tools_enabled=False
    record=active(m,direction='outbound');config=session_config(m.settings,record)
    assert {t['name'] for t in config['delegation']['responses']['tools']}=={'get_character_biography','remember_character_facts'}


def test_enabling_character_memory_never_mutates_tools_for_other_profiles(service):
    from app.calling.tools import TOOLS
    _,m,_=service;m.settings.live_tools_enabled=True
    before=json.dumps(TOOLS,sort_keys=True)
    session_config(m.settings,active(m))
    assert json.dumps(TOOLS,sort_keys=True)==before
    regular=m.personalities.save(PersonalityUpdate(name='Ordinary assistant'))
    config=session_config(m.settings,active(m,regular))
    assert not {'remember_character_facts','get_character_biography'} & {t['name'] for t in config['delegation']['responses']['tools']}


@pytest.mark.parametrize('key',['owner_name','system_prompt','permissions','identity','tool_booking','profile_override'])
def test_model_cannot_change_authority_or_owner_facts(service,key):
    _,m,_=service;record=active(m)
    with pytest.raises(ValueError):remember(m,record,key,'Override')
    assert not m.character_biographies.get(record['personality']['personality_id'])['facts']


def test_bounded_facts_values_and_operation_log(service):
    _,m,_=service;record=active(m)
    for i in range(64):remember(m,record,'hobby','Gardening',tool=str(i))
    with pytest.raises(ValueError,match='operation limit'):remember(m,record,'hobby','Gardening',tool='overflow')
    with pytest.raises(ValidationError):RememberPersonalFacts(facts=[{'key':'x','value':'x'*501}])
    with pytest.raises(ValidationError):RememberPersonalFacts(facts=[{'key':'x','value':'😀'*300}])
    with pytest.raises(ValidationError):BiographyUpdate(revision=0,facts=[{'key':'wife','value':'A'},{'key':'spouse_name','value':'B'}])
    pid=record['personality']['personality_id']
    with pytest.raises(ValueError,match='full'):replace(m,pid,revision=1,**{'topic_'+str(i):'A' for i in range(129)})
    assert canonical_key('Son’s DOB')=='sons_dob'


def test_owner_routes_are_authenticated_revision_checked_and_contact_scoped(service):
    client,m,_=service;profile=developing(m);pid=profile['personality_id'];path='/voice/personalities/'+pid+'/biography'
    assert client.get(path,headers={'Authorization':''}).status_code==401
    assert client.put(path,headers={'Authorization':''},json={'revision':0,'facts':[]}).status_code==401
    saved=client.put(path,json={'revision':0,'facts':[{'key':'hobby','value':'Gardening'}]})
    assert saved.status_code==200 and saved.json()['revision']==1
    assert client.put(path,json={'revision':0,'facts':[]}).status_code==409
    h=owner_headers(client);web='/live/personalities/'+pid+'/biography'
    assert client.get(web,headers=h).json()['facts']==saved.json()['facts']
    assert client.put(web,headers={**h,'X-CSRF-Token':''},json={'revision':1,'facts':[]}).status_code==403
    assert client.put(web,headers={**h,'Origin':'https://other.test'},json={'revision':1,'facts':[]}).status_code==403
    other=character(m)
    assert client.get('/voice/personalities/'+other['personality_id']+'/biography').json()['facts']==[]
    preview=client.post('/voice/instructions-preview',json={'personality_id':pid,'direction':'inbound'}).json()
    assert 'Gardening' in preview['voice_instructions']
    regular=m.personalities.save(PersonalityUpdate(name='Ordinary assistant'))
    assert client.get('/voice/personalities/'+regular['personality_id']+'/biography').status_code==409


def test_fiction_does_not_enter_owner_call_recall_and_characters_cannot_read_owner_history(service):
    from tests.test_call_memory import caption,previous,NUMBER
    _,m,_=service;profile=developing(m);fiction=active(m,profile,'outbound');cid=fiction['call_id']
    caption(m,cid,'assistant','My fictional favorite breakfast is pancakes with maple syrup.')
    m.transition(cid,'completed')
    real=previous(m,number=fiction['phone_number'],status='completed',objective='Real appointment reminder')
    ordinary=m.personalities.save(PersonalityUpdate(name='Ordinary assistant'))
    ongoing=active(m,ordinary,'outbound')
    rows=m.memory.records(fiction['phone_number'],ongoing['call_id'])
    assert [r['call_id'] for r in rows]==[real]
    char=active(m,profile,'outbound')
    assert m.memory.records(fiction['phone_number'],char['call_id'])==[]
    owner=m.memory.records(fiction['phone_number'])
    assert next(r for r in owner if r['call_id']==cid)['fictional_character']['personality_id']==profile['personality_id']
