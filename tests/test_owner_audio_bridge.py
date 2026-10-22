import asyncio
import base64
import json
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect

from app.calling.owner_audio_bridge import AudioBuffer,mix_private,DECODE,FRAME,SILENCE,Leg,OwnerAudioBridge
from app.calling.owner_realtime import facts_key
from app.calling.coaching_audio import linear_mulaw
from tests.test_calls import service
from tests.test_live import stream_headers
from tests.test_owner_observation import prepared,cb
from tests.test_owner_realtime import FakeSession,fact


def setup(service,monkeypatch):
    client,m,g=service
    m.settings.owner_coaching_strategy='realtime';m.settings.owner_realtime_transport='bridge'
    m.settings.owner_speech_enabled=True;m.settings.owner_coaching_application_sid=''
    client,m,g,cid,tid=prepared(service)
    m.observer.realtime.factory=FakeSession
    def unavailable(_):raise RuntimeError('caption provider unavailable')
    monkeypatch.setattr('app.calling.coaching_audio.live_client',unavailable)
    async def forbidden(*_):raise AssertionError('The two-leg bridge must not create or fetch a coach call')
    g.add_coach=forbidden;g.coach_participant=forbidden
    return client,m,g,cid,tid


def path(tid,side):return '/twilio/owner-bridge/'+tid+'/'+side


def start(m,tid,side,**extra):
    row=m.transfer.get(tid)
    return {'event':'start','start':{'accountSid':'AC123',
        'callSid':m.db.get(row['call_id'])['twilio_call_sid'] if side=='caller' else row['child_sid'],
        'streamSid':'MZ_'+side.upper(),'tracks':['inbound'],
        'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1},
        'customParameters':{'nonce':m.observer.audio_bridge.record(tid)['nonce']},**extra}}


def feed(ws,side,raw):
    ws.send_json({'event':'media','streamSid':'MZ_'+side.upper(),
        'media':{'track':'inbound','payload':base64.b64encode(raw).decode()}})


def media_until(ws,predicate,limit=100):
    for _ in range(limit):
        event=ws.receive_json()
        if event['event']=='media':
            raw=base64.b64decode(event['media']['payload'])
            if predicate(raw):return raw
    raise AssertionError('Expected audio did not arrive within the bounded packet window')


def test_pcm_mixer_has_bounded_volume_and_does_not_touch_normal_conversation():
    raw=bytes(range(256))
    assert mix_private(raw,None)==raw
    assert DECODE[0xff]==0 and DECODE[0x80]==32124 and DECODE[0]==-32124
    for sign in (-1,1):
        loud=bytes([linear_mulaw(sign*32000)])*160
        mixed=mix_private(loud,loud)
        assert all(sign*DECODE[b]>30000 for b in mixed)  # Saturation cannot wrap or reverse polarity.
    with pytest.raises(ValueError):mix_private(b'a',b'bb')
    buffer=AudioBuffer();buffer.put(b'\x90'*64000)
    assert len(buffer.data)==1600 and buffer.dropped==62400
    assert buffer.take()==b'\x90'*160


def test_bridge_uses_two_existing_legs_and_embeds_matching_inline_conference_fallback(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    assert m.transfer.get(tid)['strategy']=='bridge' and g.rings==1
    caller=ET.fromstring(g.redirects[0][1])
    owner=ET.fromstring(cb(client,m,tid,'accept',Digits='1').text)
    for xml,side in [(caller,'caller'),(owner,'owner')]:
        assert xml.find('Connect/Stream').attrib['url'].endswith(path(tid,side))
        assert xml.find('Connect/Stream/Parameter').attrib['name']=='nonce'
        conference=xml.find('Dial/Conference')
        assert conference.text=='owner-'+tid and conference.attrib['maxParticipants']=='2'
        assert xml.find('Connect') in list(xml)[:2]
        assert list(xml).index(xml.find('Connect'))<list(xml).index(xml.find('Dial'))
        assert 'app:AP' not in ET.tostring(xml,encoding='unicode')
    assert m.observer.realtime.record(tid)['participant_sid'] is None


def test_native_coaching_streams_only_to_owner_while_both_human_directions_keep_flowing(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch);pool=m.observer.realtime;fact(m)
    gate=asyncio.Event()
    class StreamingSession(FakeSession):
        async def ask(self,context,on_audio,turns=None):
            result=await super().ask(context,on_audio,turns)
            await gate.wait()  # Generation has not finished when the first private audio is delivered.
            return result
    pool.factory=StreamingSession
    observed=[];original=pool.audio_turn
    def source(tid,side,raw,complete=True):observed.append(side);original(tid,side,raw,complete)
    pool.audio_turn=source
    cp=path(tid,'caller');op=path(tid,'owner')
    with client.websocket_connect(cp,headers=stream_headers(cp,'wss')) as caller:
        caller.send_json(start(m,tid,'caller'))
        cb(client,m,tid,'accept',Digits='1')
        with client.websocket_connect(op,headers=stream_headers(op,'wss')) as owner:
            owner.send_json(start(m,tid,'owner'))
            caller_raw=b'\x90'*FRAME;owner_raw=b'\x20'*FRAME
            feed(caller,'caller',caller_raw);feed(owner,'owner',owner_raw)
            assert media_until(owner,lambda raw:raw==caller_raw)==caller_raw
            assert media_until(caller,lambda raw:raw==owner_raw)==owner_raw
            # The real native-turn path ends a verified caller utterance without relying on captions.
            for raw in [caller_raw]*3+[SILENCE]*22:feed(caller,'caller',raw)
            private=media_until(owner,lambda raw:any(DECODE[b]>30000 for b in raw))
            assert private and not gate.is_set()
            ch=pool.channels[tid]
            assert ch.job and not ch.job.done()
            assert ch.session.contexts[-1]['audio_sources'][-1]=='caller'
            for _ in range(8):
                event=caller.receive_json()
                assert event['event']=='media'
                assert base64.b64decode(event['media']['payload']) in (owner_raw,SILENCE)
            # Keep speaker identity on the OWNER raw microphone as well.
            for raw in [owner_raw]*3+[SILENCE]*22:feed(owner,'owner',raw)
            async def finish():gate.set();await asyncio.sleep(.05)
            client.portal.call(finish)
            assert 'caller' in observed and 'owner' in observed
            assert m.transfer.get(tid)['transcription_state']=='degraded'  # Captions failed; native relay/coach did not.
            assert m.db.get(cid)['status']=='in_progress' and g.rings==1


@pytest.mark.parametrize('fault',['bad_nonce','wrong_call','wrong_format','wrong_account'])
def test_forged_or_cross_leg_start_cannot_attach_to_or_interrupt_a_bridge(service,monkeypatch,fault):
    client,m,g,cid,tid=setup(service,monkeypatch)
    p=path(tid,'caller');changes={
        'bad_nonce':{'customParameters':{'nonce':'wrong'}},
        'wrong_call':{'callSid':m.transfer.get(tid)['child_sid']},
        'wrong_format':{'mediaFormat':{'encoding':'audio/pcm','sampleRate':24000,'channels':1}},
        'wrong_account':{'accountSid':'ACwrong'}}[fault]
    with client.websocket_connect(p,headers=stream_headers(p,'wss')) as ws:
        ws.send_json(start(m,tid,'caller',**changes))
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    assert m.observer.audio_bridge.record(tid)['state']=='waiting'
    assert not m.observer.audio_bridge.rooms[tid].legs and m.db.get(cid)['status']=='in_progress'
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(p):pass


def test_bridge_connection_failure_closes_peer_to_execute_inline_fallback_without_hanging_up_humans(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    cp=path(tid,'caller');op=path(tid,'owner')
    with client.websocket_connect(cp,headers=stream_headers(cp,'wss')) as caller:
        caller.send_json(start(m,tid,'caller'));cb(client,m,tid,'accept',Digits='1')
        with client.websocket_connect(op,headers=stream_headers(op,'wss')) as owner:
            owner.send_json(start(m,tid,'owner'))
            media_until(owner,lambda _:True)
            caller.close()
            with pytest.raises(WebSocketDisconnect):
                for _ in range(100):owner.receive_json()
    assert m.observer.audio_bridge.record(tid)['state']=='fallback'
    assert m.transfer.get(tid)['mode']=='transcript'
    assert tid not in m.observer.realtime.channels
    assert m.db.get(cid)['status']=='in_progress' and g.rings==1
    xml=m.observer.conference_twiml(m.transfer.get(tid),'owner')
    assert '<Connect>' not in xml and '<Conference' in xml and 'maxParticipants="2"' in xml


def test_simple_answer_uses_normal_two_person_conference_and_never_starts_a_coaching_model(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    before=len(FakeSession.instances)
    response=cb(client,m,tid,'accept',Digits='3')
    assert response.status_code==200 and '<Connect>' not in response.text and '<Conference' in response.text
    assert m.transfer.get(tid)['mode']=='transcript' and m.observer.audio_bridge.in_fallback(tid)
    assert not m.observer.realtime.record(tid) and len(FakeSession.instances)==before


def test_provider_unavailable_keeps_both_human_audio_paths_running(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    class UnavailableSession(FakeSession):
        def __init__(self,*args):super().__init__(*args);self.closed.set()
    m.observer.realtime.factory=UnavailableSession
    cp=path(tid,'caller');op=path(tid,'owner')
    with client.websocket_connect(cp,headers=stream_headers(cp,'wss')) as caller:
        caller.send_json(start(m,tid,'caller'));cb(client,m,tid,'accept',Digits='1')
        with client.websocket_connect(op,headers=stream_headers(op,'wss')) as owner:
            owner.send_json(start(m,tid,'owner'))
            feed(caller,'caller',b'\x90'*FRAME);feed(owner,'owner',b'\x20'*FRAME)
            assert media_until(owner,lambda raw:raw==b'\x90'*FRAME)
            assert media_until(caller,lambda raw:raw==b'\x20'*FRAME)
            assert m.db.get(cid)['status']=='in_progress'
            assert m.observer.audio_bridge.record(tid)['state']=='active'


def test_revoked_context_and_pause_withhold_mixed_audio_and_clear_only_the_owner(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch);body=fact(m)
    cb(client,m,tid,'accept',Digits='1')
    pool=m.observer.realtime;ch=pool.channels[tid];room=m.observer.audio_bridge.rooms[tid]
    class Socket:
        def __init__(self):self.messages=[]
        async def send_json(self,data):self.messages.append(data)
        async def close(self):pass
    owner=Socket();caller=Socket()
    room.legs={'caller':Leg(caller,'MZ_CALLER'),'owner':Leg(owner,'MZ_OWNER')}
    snapshot=facts_key(pool.context(tid,None)['CURRENT_APPROVED_FACTS'])
    hint=(ch.generation,snapshot,b'\x80'*FRAME)
    ch.delivery_permission=(ch.generation,snapshot)
    ch.delivery_until=__import__('time').monotonic()+30
    async def check():
        await room.send('owner',SILENCE,hint)
        assert base64.b64decode(owner.messages[-1]['media']['payload'])!=SILENCE
        m.observer.paused.add(tid)
        await room.send('owner',SILENCE,hint)
        assert base64.b64decode(owner.messages[-1]['media']['payload'])==SILENCE
        await pool.clear(ch)
        assert owner.messages[-1]['event']=='clear' and not caller.messages
        m.observer.paused.discard(tid);m.knowledge.retire(str(body.fact_id))
        await room.send('owner',SILENCE,hint)
        assert base64.b64decode(owner.messages[-1]['media']['payload'])==SILENCE
        await room.send('caller',b'\x20'*FRAME,hint,marks=['forged-mark'])
        assert len(caller.messages)==1 and base64.b64decode(caller.messages[0]['media']['payload'])==b'\x20'*FRAME
    client.portal.call(check)


def test_raw_model_feed_error_does_not_break_the_human_relay(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    def unavailable(*_,**__):raise RuntimeError('model processing unavailable')
    m.observer.realtime.audio_turn=unavailable
    cp=path(tid,'caller');op=path(tid,'owner')
    with client.websocket_connect(cp,headers=stream_headers(cp,'wss')) as caller:
        caller.send_json(start(m,tid,'caller'));cb(client,m,tid,'accept',Digits='1')
        with client.websocket_connect(op,headers=stream_headers(op,'wss')) as owner:
            owner.send_json(start(m,tid,'owner'))
            for raw in [b'\x90'*FRAME]*3+[SILENCE]*22:feed(caller,'caller',raw)
            # Processing an utterance stops coaching, while fresh human media still crosses.
            async def settle():await asyncio.sleep(.1)
            client.portal.call(settle)
            feed(caller,'caller',b'\x90'*FRAME);feed(owner,'owner',b'\x20'*FRAME)
            assert media_until(owner,lambda raw:raw==b'\x90'*FRAME)
            assert media_until(caller,lambda raw:raw==b'\x20'*FRAME)
            assert m.observer.audio_bridge.record(tid)['state']=='active'


def test_restart_keeps_the_fallback_decision_and_never_rebuilds_an_old_live_bridge(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    replacement=OwnerAudioBridge(m.observer)
    assert replacement.in_fallback(tid) and replacement.prepare(tid)['state']=='fallback'
    assert not replacement.rooms and g.rings==1


def test_owner_hangup_stops_both_relay_sockets_and_blocks_keypad_redirects_during_handoff(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    cp=path(tid,'caller');op=path(tid,'owner')
    with client.websocket_connect(cp,headers=stream_headers(cp,'wss')) as caller:
        caller.send_json(start(m,tid,'caller'));cb(client,m,tid,'accept',Digits='1')
        with client.websocket_connect(op,headers=stream_headers(op,'wss')) as owner:
            owner.send_json(start(m,tid,'owner'))
            media_until(owner,lambda _:True)
            assert client.post('/calls/'+cid+'/digits',json={'digits':'1','reason':'No redirect during handoff'}).status_code==409
            assert client.post('/calls/'+cid+'/hangup').status_code==200
            with pytest.raises(WebSocketDisconnect):
                for _ in range(100):owner.receive_json()
    assert m.observer.audio_bridge.record(tid)['state']=='completed'
    assert cid in m.stopping and m.db.get(cid)['status'] in ('completed','cancelled')
    assert tid not in m.observer.audio_bridge.rooms and g.rings==1
