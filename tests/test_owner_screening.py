import asyncio
import time
import xml.etree.ElementTree as ET

from twilio.request_validator import RequestValidator

from app.calling.owner_screening import PROMPTS,prompt_key
from tests.test_calls import service
from tests.test_owner_observation import prepared,cb
from tests.test_owner_speech import wav_bytes


def test_short_menu_uses_cached_speech_and_never_generates_per_callback(service,tmp_path):
    client,m,g,cid,tid=prepared(service)
    m.settings.owner_screen_speech_enabled=True
    m.settings.owner_prompt_directory=tmp_path
    (tmp_path/prompt_key(m.settings,'menu')).write_bytes(wav_bytes())
    xml=cb(client,m,tid,'screen').text
    assert '<Play>' in xml and PROMPTS['menu'] not in xml
    path=ET.fromstring(xml).find('Gather/Play').text.removeprefix('https://example.test')
    assert client.get(path).status_code==403
    signature=RequestValidator('twilio-test').compute_signature('https://example.test'+path,{})
    response=client.get(path,headers={'X-Twilio-Signature':signature})
    assert response.status_code==200 and response.content==wav_bytes()
    assert 'no-store' in response.headers['cache-control']
    cb(client,m,tid,'accept',Digits='3')
    assert client.get(path,headers={'X-Twilio-Signature':signature}).status_code==404


def test_brief_is_prepared_once_while_ringing_and_never_blocks_screening(service):
    client,m,g,cid,tid=prepared(service)
    m.settings.owner_screen_speech_enabled=True
    generated=[]
    async def speech(settings,text):generated.append(text);return wav_bytes()
    m.screening.speech=speech
    async def prepare():
        m.screening.prepare(tid);m.screening.prepare(tid)
        await asyncio.gather(*m.screening.tasks.values())
        m.screening.prepare(tid)
    asyncio.run(prepare())
    assert generated==['A complex question needs the owner.']
    xml=cb(client,m,tid,'screen').text
    assert '/prompt/brief.wav' in xml and generated[0] not in xml
    m.screening.briefs[tid]['expires']=time.monotonic()-1
    assert generated[0] in cb(client,m,tid,'screen').text


def test_missing_or_bad_speech_assets_use_short_say_fallback(service,tmp_path):
    client,m,g,cid,tid=prepared(service)
    m.settings.owner_screen_speech_enabled=True
    m.settings.owner_prompt_directory=tmp_path
    (tmp_path/prompt_key(m.settings,'menu')).write_bytes(b'broken')
    assert PROMPTS['menu'] in cb(client,m,tid,'screen').text
    async def fail(*_):raise RuntimeError('private provider details')
    m.screening.speech=fail
    async def prepare():
        m.screening.prepare(tid)
        await asyncio.gather(*m.screening.tasks.values())
    asyncio.run(prepare())
    assert not m.screening.briefs and g.rings==1
    assert 'private provider details' not in cb(client,m,tid,'screen').text


def test_answer_still_connects_without_luna_and_simple_mode_keeps_captions(service):
    client,m,g,cid,tid=prepared(service)
    m.settings.subscription_lookup_enabled=False
    accepted=cb(client,m,tid,'accept',Digits='1')
    assert '<Conference' in accepted.text and '<Start><Stream' in accepted.text
    assert m.transfer.get(tid)['mode']=='transcript'


def test_cache_key_changes_with_model_or_voice(service):
    _,m,_=service
    first=prompt_key(m.settings,'menu')
    m.settings.owner_screen_speech_voice='marin'
    assert first!=prompt_key(m.settings,'menu')
