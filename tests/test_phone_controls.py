"""Fictional owner controls, inheritance, migration and immutable notice tests."""
import asyncio
import io
import sqlite3
import wave
from uuid import uuid4
import xml.etree.ElementTree as ET
import pytest
from pydantic import SecretStr
from app.calling.voice_backends import BackendChoice, ContactVoiceChoice, VoiceBackends
from app.calling.recording_settings import RecordingChoice
from app.calling.knowledge import ContactUpdate
from app.calling.recordings import CallRecordings
from app.calling.prompts import session_config
from tests.test_calls import service,create
from tests.test_recordings import incoming,enable,signed,SID
from tests.test_voice_backends import configure


def owner_headers(client):
    auth=client.app.state.web_auth
    token=auth.login(auth.create_code());session=auth.session(token)
    return {'Cookie':'__Host-phone-owner='+token,'Origin':'https://example.test','X-CSRF-Token':session['csrf']}

def contact(m,number='+12025550101',name='Fictional caller'):
    body=ContactUpdate(name=name,phone_number=number)
    m.knowledge.contact(body);return body.contact_id

def pcm_wave(seconds=.1,rate=16000):
    output=io.BytesIO()
    with wave.open(output,'wb') as wav:
        wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(b'\0\0'*int(rate*seconds))
    return output.getvalue()

@pytest.mark.parametrize('body',[
    {'provider':'openai','model':'imagined-model'},
    {'provider':'openai','voice':'fictional-clone'},
    {'provider':'elevenlabs','model':'gpt-live-1'},
    {'provider':'elevenlabs','voice':'marin'},
    {'provider':'gemini','voice':'fictional-clone'},
    {'provider':'gemini','model':'old-preview'}])
def test_cross_provider_choices_rejected_before_dial(service,body):
    c,m,g=service;configure(m)
    assert c.put('/voice/backend',json=body).status_code==409
    assert m.voice_backends.get()['provider']=='openai' and not g.calls


def test_legacy_voice_api_and_models_remain_compatible(service):
    c,m,_=service;configure(m)
    assert c.put('/voice/backend',json={'provider':'elevenlabs','elevenlabs_model':'eleven_realtime_v1_max'}).status_code==200
    assert c.put('/voice/backend',json={'provider':'openai','voice':'gleam'}).status_code==200
    assert c.put('/voice',json={'voice':'marin'}).status_code==200
    assert m.voice_backends.get()['voice']=='marin'
    assert c.put('/voice/backend',json={'provider':'elevenlabs'}).json()['model']=='eleven_realtime_v1_max'


def test_contact_inherits_provider_local_defaults_and_pins_before_dial(service):
    c,m,_=service;configure(m);cid=contact(m)
    m.voice_backends.set_contact(cid,ContactVoiceChoice(provider='elevenlabs'))
    call=create(c).json()['call_id'];selected=m.voice_backends.for_call(call)
    assert selected['provider']=='elevenlabs' and selected['voice']=='test-clone'
    m.voice_backends.set(BackendChoice(provider='elevenlabs',model='eleven_realtime_v1_max'))
    m.voice_backends.set(BackendChoice(provider='openai',voice='gleam'))
    assert m.voice_backends.pin(m.db.get(call))==selected
    assert m.voice_backends.contact_result(cid)['effective']['model']=='eleven_realtime_v1_max'
    m.voice_backends.set_contact(cid,ContactVoiceChoice())
    assert m.voice_backends.contact_result(cid)['effective']['voice']=='gleam'
    assert m.voice_backends.contact(cid)=={'provider':None,'model':None,'voice':None}
    assert create(c).status_code==201


def test_override_highest_priority_and_never_carries_foreign_voice(service):
    c,m,_=service;configure(m);cid=contact(m)
    m.voice_backends.set_contact(cid,ContactVoiceChoice(provider='elevenlabs',voice='test-clone'))
    r=c.post('/calls',json={'phone_number':'+12025550101','objective':'Fictional check',
        'voice_provider':'openai','voice':'gleam'})
    assert r.status_code==201
    assert m.voice_backends.for_call(r.json()['call_id'])['voice']=='gleam'
    r=c.post('/calls',json={'phone_number':'+12025550101','objective':'Fictional check','voice_provider':'openai','voice':'test-clone'})
    assert r.status_code==503


def test_ambiguous_contact_uses_global_and_inheritance_validation(service):
    c,m,_=service;configure(m);a=contact(m);contact(m,name='Other fictional caller')
    m.voice_backends.set_contact(a,ContactVoiceChoice(provider='elevenlabs'))
    assert m.voice_backends.for_call(create(c).json()['call_id'])['provider']=='openai'
    with pytest.raises(ValueError,match='provider'):
        m.voice_backends.set_contact(a,ContactVoiceChoice(voice='marin'))


def test_forwarding_uses_original_presented_caller_not_owner(service):
    c,m,_=service;configure(m);m.settings.owner_transfer_number='+12025550109'
    original=contact(m,number='+12025550102');owner=contact(m,number='+12025550109')
    m.voice_backends.set_contact(owner,ContactVoiceChoice(provider='elevenlabs'))
    incoming(c,m,From='+12025550102',ForwardedFrom='+12025550109')
    call=m.db.by_twilio_sid(SID)['call_id']
    assert m.voice_backends.for_call(call)['provider']=='openai'
    m.voice_backends.set_contact(original,ContactVoiceChoice(provider='elevenlabs'))
    # Different inbound leg preserving original caller ID selects that caller.
    m.transition(call,'completed')
    incoming(c,m,From='+12025550102',CallSid='CA'+'3'*32,ForwardedFrom='+12025550109')
    call=m.db.by_twilio_sid('CA'+'3'*32)['call_id']
    assert m.voice_backends.for_call(call)['provider']=='elevenlabs'
    m.transition(call,'completed')
    incoming(c,m,CallSid='CA'+'4'*32,From='+12025550109',ForwardedFrom='+12025550109')
    call=m.db.by_twilio_sid('CA'+'4'*32)['call_id']
    assert m.voice_backends.for_call(call)['provider']=='openai'


@pytest.mark.parametrize('recording,announcement',[(True,True),(True,False),(False,True),(False,False)])
def test_recording_notice_matrix_and_truthful_prompt(service,tmp_path,recording,announcement):
    c,m,_=service;enable(m,tmp_path)
    m.recordings.preferences.set(RecordingChoice(recording_enabled=recording,announcement_enabled=announcement))
    response=incoming(c,m);cid=m.db.by_twilio_sid(SID)['call_id'];root=ET.fromstring(response.text)
    assert (root.find('Start/Recording') is not None)==recording
    assert (root.find('Play') is not None)==(recording and announcement)
    snapshot=m.recordings.snapshot(cid);policy=snapshot['policy']
    if policy:
        assert bool(policy['notice_issued_at'])==(recording and announcement)
        assert bool(policy['recording_requested_at'])==recording
    config=session_config(m.settings,m.voice_record(cid));prompt=config['instructions']
    assert ('did NOT play' in prompt)==(not (recording and announcement))
    if not recording: assert 'recording is OFF' in prompt
    if not announcement: assert 'announcements are disabled' in prompt
    assert '<Recording' not in incoming(c,m).text and '<Play' not in incoming(c,m).text
    assert snapshot['pinned_settings']['announcement_enabled']==announcement


def test_silent_recording_does_not_require_cached_notice_and_policy_persists(service,tmp_path):
    c,m,_=service
    m.settings.recording_notice_directory=tmp_path/'missing'
    m.recordings.preferences.set(RecordingChoice(recording_enabled=True,announcement_enabled=False))
    first=create(c).json()['call_id']
    m.recordings.preferences.set(RecordingChoice(recording_enabled=False,announcement_enabled=True))
    xml=signed(c,'/twilio/voice/'+first,{'AccountSid':'AC123','CallSid':'CA123'}).text
    assert '<Recording' in xml and '<Play' not in xml
    m.db.update(first,twilio_call_sid='CA_SYNTHETIC_OLD')
    second=create(c).json()['call_id']
    assert '<Recording' not in signed(c,'/twilio/voice/'+second,{'AccountSid':'AC123','CallSid':'CA123'}).text
    restarted=CallRecordings(m)
    assert restarted.snapshot(first)['policy']['notice_issued_at'] is None
    assert restarted.policy_for(first)['recording_enabled'] is True
    assert restarted.preferences.get()['announcement_enabled'] is True


def test_opt_out_before_notice_can_never_restart(service,tmp_path):
    c,m,_=service;enable(m,tmp_path);cid=create(c).json()['call_id']
    assert asyncio.run(m.recordings.stop(cid))['status']=='not_recording'
    assert '<Recording' not in signed(c,'/twilio/voice/'+cid,{'AccountSid':'AC123','CallSid':'CA123'}).text
    assert m.recordings.snapshot(cid)['policy']['recording_requested_at'] is None


def test_legacy_recording_policy_migrates_without_losing_optout(service):
    c,m,_=service;cid=create(c).json()['call_id']
    with m.db.connect() as db:
        db.execute('DROP TABLE call_recording_policy')
        db.execute('CREATE TABLE call_recording_policy(call_id TEXT PRIMARY KEY,notice_issued_at TEXT NOT NULL,stop_requested_at TEXT,stop_confirmed_at TEXT,error_code TEXT)')
        db.execute('INSERT INTO call_recording_policy VALUES (?,?,?,?,?)',(cid,'old-notice','old-request','old-stop',None))
    restarted=CallRecordings(m)
    assert restarted.snapshot(cid)['policy']['stop_confirmed_at']=='old-stop'
    assert restarted.snapshot(cid)['policy']['recording_requested_at']=='old-notice'
    with m.db.connect() as db:
        assert not next(r for r in db.execute('pragma table_info(call_recording_policy)') if r['name']=='notice_issued_at')['notnull']
    assert CallRecordings(m).snapshot(cid)['policy']==restarted.snapshot(cid)['policy']


def test_notices_authenticated_atomic_versioned_no_generation_on_playback(service,tmp_path,monkeypatch):
    c,m,_=service;enable(m,tmp_path);headers=owner_headers(c)
    async def no_tts(*_): raise AssertionError('TTS during ordinary playback')
    monkeypatch.setattr('scripts.prepare_recording_notices.render_notice',no_tts)
    assert c.get('/live/recording-notices').status_code==401
    assert c.get('/live/recording-notices/en/audio').status_code==401
    assert c.post('/live/recording-notices/en/upload',content=pcm_wave()).status_code==401
    pref=m.recordings.preferences
    first=pref.stage('en','This call is being recorded.',pcm_wave());asset=first[0]['draft']['asset_id']
    assert c.get('/live/recording-notices/en/audio?asset_id='+asset,headers=headers).status_code==200
    assert c.post('/live/recording-notices/en/activate',json={'asset_id':asset},headers=headers).status_code==200
    incoming_xml=incoming(c,m).text;url=ET.fromstring(incoming_xml).find('Play').text
    assert '?v=' in url and c.get(url).status_code==403
    from twilio.request_validator import RequestValidator
    signed_headers={'X-Twilio-Signature':RequestValidator('twilio-test').compute_signature(url,{})}
    before=c.get(url,headers=signed_headers).content
    pref.stage('en','This conversation is recorded.',pcm_wave(.2));new=pref.notices()[0]['draft']['asset_id'];pref.activate('en',new)
    assert c.get(url,headers=signed_headers).content==before
    assert c.get(url+'&extra=changed',headers=signed_headers).status_code==403
    assert c.post('/live/recording-notices/en/generate',json={'text':'Please record this call.'},headers=headers).status_code==502
    assert pref.notices()[0]['active_asset']['asset_id']==new
    assert c.post('/live/recording-notices/en/upload?text=Notice',content=b'not-wav',headers=headers).status_code==422
    assert pref.notices()[0]['active_asset']['asset_id']==new
    assert c.post('/live/recording-notices/en/upload?text=Notice',content=b'x'*(2*1024*1024+1),headers=headers).status_code==413
    assert '<Play' not in incoming(c,m).text


@pytest.mark.parametrize('audio',[b'',b'bad',pcm_wave(8.01),pcm_wave(rate=4000)])
def test_notice_upload_bounds(service,audio):
    _,m,_=service
    with pytest.raises(ValueError):m.recordings.preferences.normalize(audio)


def test_controls_csrf_and_real_session_persistence(service):
    c,m,_=service;headers=owner_headers(c)
    no_csrf={k:v for k,v in headers.items() if k!='X-CSRF-Token'}
    body={'recording_enabled':True,'announcement_enabled':False}
    assert c.put('/live/recording-settings',json=body,headers=no_csrf).status_code==403
    assert c.put('/live/recording-settings',json=body,headers={**headers,'Origin':'https://untrusted.test'}).status_code==403
    assert c.put('/live/recording-settings',json=body,headers=headers).status_code==200
    cid=contact(m)
    assert c.put('/live/contacts/'+str(cid)+'/voice',json={'provider':'openai','voice':'gleam'},headers=headers).status_code==200
    bootstrap=c.get('/live/bootstrap',headers=headers).json()
    assert bootstrap['recording_settings']['announcement_enabled'] is False
    assert bootstrap['caller_book'][0]['answering']['effective']['voice']=='gleam'
    assert c.get('/live',headers=headers).status_code==200


def test_opening_claim_is_durable_across_sessions(service):
    c,m,_=service;cid=create(c).json()['call_id']
    assert m.voice_backends.claim_opening(cid)
    assert not VoiceBackends(m.db,m.settings,m).claim_opening(cid)
    assert m.voice_record(cid)['voice_opening_claimed'] is True


def test_library_catalog_refresh_is_bounded_private_and_atomic(service,monkeypatch):
    import httpx
    c,m,_=service;configure(m)
    real_client=httpx.AsyncClient
    def respond(request):
        assert request.url.path=='/v2/voices' and request.url.params['voice_type']=='saved'
        return httpx.Response(200,json={'voices':[{'voice_id':'fictional-second','name':'Fictional saved voice'}],'has_more':False})
    monkeypatch.setattr('app.calling.voice_backends.httpx.AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw))
    result=asyncio.run(m.voice_backends.refresh_elevenlabs())
    voices=result['providers'][1]['models'][0]['voices']
    assert {v['id'] for v in voices}=={'test-clone','fictional-second'}
    assert 'test-key' not in str(result)
    m.voice_backends.set(BackendChoice(provider='elevenlabs',voice='fictional-second'))
    assert m.voice_backends.get()['voice']=='fictional-second'
    def fail(_):return httpx.Response(503)
    monkeypatch.setattr('app.calling.voice_backends.httpx.AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(fail),**kw))
    with pytest.raises(ValueError):asyncio.run(m.voice_backends.refresh_elevenlabs())
    assert m.voice_backends.get()['providers'][1]['models'][0]['voices']==voices


def test_existing_mcp_names_and_extended_controls(service):
    import json
    from app.mcp.server import create_mcp
    c,m,_=service;cid=contact(m);m.settings.oauth_database_path=m.db.path.parent/'oauth-test.sqlite3'
    server,_=create_mcp(m)
    async def run():
        async def call(name,args):
            result=await server.call_tool(name,args)
            assert not result.is_error
            return json.loads(result.content[0].text)
        assert (await call('set_voice',{'voice':'gleam'}))['voice']=='gleam'
        assert (await call('set_voice_backend',{'provider':'openai'}))['voice']=='gleam'
        assert (await call('set_recording_settings',{'recording_enabled':False,'announcement_enabled':False}))['recording_enabled'] is False
        assert (await call('get_recording_settings',{}))['announcement_enabled'] is False
        await call('set_contact_voice',{'contact_id':str(cid),'provider':'openai','voice':'marin'})
        assert (await call('get_contact_voice',{'contact_id':str(cid)}))['effective']['voice']=='marin'
        assert len((await call('get_recording_notices',{}))['notices'])==2
    asyncio.run(run())
