"""Speaker-name compatibility and real-history forwarding suggestions."""
import json
from uuid import uuid4

import pytest

from app.calling.personalities import PersonalityUpdate
from app.calling.presentation import resolve_presentation, voice_identity
from app.calling.inbound import startup_greeting_content
from app.storage.models import CallRequest
from tests.test_calls import service
from tests.test_phone_controls import contact, owner_headers
from tests.test_contact_guidance import call_record, profile


@pytest.mark.parametrize('mode,custom,expected', [('assistant','','Rocky'),('assistant','Nova','Nova'),
    ('owner_first_person','','Alex'),('owner_first_person','Sam','Sam')])
def test_names_are_mode_specific_and_pinned(service,mode,custom,expected):
    _,manager,_=service; manager.settings.assistant_name='Rocky'
    chosen=profile(manager,mode=mode,spoken_name=custom)
    record=call_record(manager,chosen,'inbound')
    assert resolve_presentation(manager.settings,record)['spoken_name']==expected
    assert expected in voice_identity(manager.settings,record,inbound=True)
    assert expected in startup_greeting_content(manager.settings,record)
    assert record['personality']['name']==chosen['name']
    manager.settings.assistant_name='Changed assistant'
    manager.settings.caller_name='Changed owner'
    assert resolve_presentation(manager.settings,manager.voice_record(record['call_id']))['spoken_name']==expected


def test_legacy_assistant_owner_fallback_is_not_reinterpreted_as_a_name(service):
    _,manager,_=service; manager.settings.assistant_name='Rocky'
    chosen=profile(manager,spoken_name='')
    record=call_record(manager,chosen)
    legacy={**record['personality'],'spoken_name':'Alex'};legacy.pop('identity_version')
    with manager.db.connect() as db:
        db.execute('UPDATE call_personalities SET snapshot=? WHERE call_id=?',(json.dumps(legacy),record['call_id']))
    current=manager.voice_record(record['call_id'])
    assert resolve_presentation(manager.settings,current)['spoken_name']=='Rocky'
    assert "You are Rocky," in voice_identity(manager.settings,current)
    with manager.db.connect() as db:
        assert json.loads(db.execute('SELECT snapshot FROM call_personalities WHERE call_id=?',(record['call_id'],)).fetchone()[0])['spoken_name']=='Alex'


def test_saved_name_is_editable_through_owner_api_in_assistant_mode(service):
    client,manager,_=service;headers=owner_headers(client)
    chosen=profile(manager)
    result=client.put('/live/personalities/'+chosen['personality_id'],headers=headers,
        json={'name':chosen['name'],'revision':chosen['revision'],'presentation_mode':'assistant','spoken_name':'Nova'})
    assert result.status_code==200 and result.json()['spoken_name']=='Nova'
    assert manager.personalities.effective({'direction':'outbound','phone_number':''},chosen['personality_id'])['spoken_name']=='Nova'


def real_call(manager,number,forwarded='',stamp='2026-11-01T12:00:00+00:00'):
    cid=manager.db.create(CallRequest(phone_number=number,objective='Fictional historical call'))
    with manager.db.connect() as db:
        db.execute('UPDATE calls SET created_at=? WHERE call_id=?',(stamp,cid))
    manager.routing.save(cid,{'ForwardedFrom':forwarded})
    manager.transition(cid,'completed')
    return cid


def test_recent_sources_are_private_deduplicated_sorted_and_labeled(service):
    client,manager,_=service
    contact(manager,number='+12025550109',name='Source Example')
    real_call(manager,'+12025550101','+12025550109')
    real_call(manager,'+12025550102','+12025550109','2026-11-02T12:00:00+00:00')
    real_call(manager,'+12025550103','+12025550108','2026-11-03T12:00:00+00:00')
    real_call(manager,'+12025550109',stamp='2026-11-04T12:00:00+00:00')
    endpoint='/live/browser-tests/recent-numbers'
    assert client.get(endpoint).status_code==401
    result=client.get(endpoint,headers=owner_headers(client)).json()
    assert [r['number'] for r in result['forwarding_sources']]==['+12025550108','+12025550109']
    assert result['forwarding_sources'][1]['contact_name']=='Source Example'
    assert [r['number'] for r in result['call_numbers']]==['+12025550103','+12025550102','+12025550101']


def test_recent_history_limits_and_browser_test_exclusion(service):
    client,manager,_=service
    excluded=real_call(manager,'+12025550199','+12025550198','2026-11-05T12:00:00+00:00')
    with manager.db.connect() as db:
        db.execute('INSERT INTO browser_test_calls(call_id,request_id,owner_hash,fingerprint,scenario,created_at) VALUES (?,?,?,?,?,?)',
            (excluded,str(uuid4()),'fixture','fixture','forwarded',0))
    for index in range(100):
        real_call(manager,'+'+'120255501'+f'{index:02d}','+'+'120255501'+f'{(index+1)%100:02d}',
            f'2026-11-06T12:{index//60:02d}:{index%60:02d}+00:00')
    result=manager.browser_tests.recent_numbers()
    assert len(result['forwarding_sources'])==20
    assert len(result['call_numbers'])<=20
    assert all(r['last_seen'].startswith('2026-11-06') for group in result.values() for r in group)
