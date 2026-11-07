"""Source isolation, owner auth and real-time mixing with no carrier calls."""
import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocketDisconnect

from app.calling.browser_audio import AudioHub, Listener, mix_audio, transcribe_owner
from tests.test_calls import service
from tests.test_live import FakeClient, FakeConnection, Event
from tests.test_browser_tests import start, no_phone
from tests.test_phone_controls import owner_headers


class Socket:
    def __init__(self): self.sent=[]; self.input=asyncio.Queue()
    async def send_json(self, message): self.sent.append(message)
    async def receive_json(self): return await self.input.get()
    async def close(self): pass


def make_hub():
    socket=Socket()
    manager=SimpleNamespace(settings=SimpleNamespace(openai_api_key=SimpleNamespace(get_secret_value=lambda:'fake')),
        stopping=set(), db=SimpleNamespace(get=lambda cid:{'status':'in_progress','transcript':[]}, human_caption=lambda *args:None))
    hub=AudioHub(socket, manager, 'one', 'stream')
    bridge=SimpleNamespace(closing=False,handoff=False,end_requested=asyncio.Event(),connection=SimpleNamespace())
    bridge.clear_live_audio=lambda:hub.send_json({'event':'clear','streamSid':'stream'})
    hub.bridge=bridge
    return hub,socket


def test_taps_are_separate_and_never_block_the_phone():
    async def run():
        hub,socket=make_hub(); listener=Listener();hub.listeners.add(listener)
        raw=bytes([160])*160
        await socket.input.put({'event':'media','streamSid':'stream','media':{'track':'inbound','payload':base64.b64encode(raw).decode()}})
        message=await hub.receive_json()
        assert message['media']['track']=='inbound'
        assert (await listener.queue.get())['source']=='caller'
        await hub.send_json({'event':'media','media':{'payload':base64.b64encode(raw).decode()}})
        assert listener.queue.get_nowait()['source']=='assistant'
        for _ in range(102): hub.publish('caller',raw)
        assert listener.failed.is_set() and listener.queue.qsize()==100
        assert len(socket.sent)==1
    asyncio.run(run())


def test_mixer_is_paced_marks_follow_audio_and_owner_does_not_hear_self():
    async def run():
        hub,socket=make_hub();owner=Listener();other=Listener();hub.listeners.update((owner,other))
        await hub.join(owner)
        owner.queue.get_nowait();other.queue.get_nowait()  # switching clears old assistant audio
        owner_raw=bytes([190])*160;assistant=bytes([170])*320
        hub.microphone(owner,owner_raw)
        await hub.send_json({'event':'media','media':{'payload':base64.b64encode(assistant).decode()}})
        await hub.send_json({'event':'mark','mark':{'name':'done'}})
        await asyncio.sleep(.075)
        media=[m for m in socket.sent if m['event']=='media']
        assert len(media)==2
        assert base64.b64decode(media[0]['media']['payload'])==mix_audio(assistant[:160],owner_raw)
        assert base64.b64decode(media[1]['media']['payload'])==assistant[160:]
        assert socket.sent[-1]['event']=='mark'
        assert all(m['source']!='owner' for m in list(owner.queue._queue))
        assert any(m.get('source')=='owner' for m in list(other.queue._queue))
        assert hub.owner_queue.get_nowait()==owner_raw
        hub.leave(owner);assert not hub.owner_audio and hub.owner is None
        await hub.close()
    asyncio.run(run())


def test_clear_drops_stale_assistant_marks_without_discarding_owner_microphone():
    async def run():
        hub,socket=make_hub();owner=Listener();await hub.join(owner)
        hub.microphone(owner,bytes([190])*160)
        await hub.send_json({'event':'media','media':{'payload':base64.b64encode(bytes([170])*800).decode()}})
        await hub.send_json({'event':'mark','mark':{'name':'stale'}})
        await hub.send_json({'event':'clear'})
        assert not hub.output and not hub.marks and len(hub.owner_audio)==160
        await asyncio.sleep(.04)
        assert [m['event'] for m in socket.sent]==['clear','clear','media']
        assert base64.b64decode(socket.sent[-1]['media']['payload'])==bytes([190])*160
        await hub.close()
    asyncio.run(run())


def test_join_bounds_and_handoff_mute():
    async def run():
        hub,_=make_hub();one,two=Listener(),Listener()
        await hub.join(one)
        with pytest.raises(ValueError,match='already'): await hub.join(two)
        with pytest.raises(ValueError,match='not joined'): hub.microphone(two,b'x')
        with pytest.raises(ValueError,match='faster'): hub.microphone(one,b'x'*1601)
        hub.bridge.handoff=True
        with pytest.raises(ValueError,match='not joined'): hub.microphone(one,b'x')
        await asyncio.sleep(.04);assert not hub.owner_audio and one.failed.is_set()
        await hub.close()
    asyncio.run(run())


def test_mix_saturates_without_integer_wrap():
    from app.calling.live_session import mulaw_sample
    loud=bytes([128])*160
    assert all(mulaw_sample(b)>30000 for b in mix_audio(loud,loud))


def test_live_audio_requires_owner_and_origin(service):
    client,manager,_=service
    cid='00000000-0000-4000-8000-000000000000'
    assert client.get('/live/calls/'+cid+'/live-audio').status_code==401
    headers=owner_headers(client)
    assert client.get('/live/calls/'+cid+'/live-audio',headers=headers).json()['available'] is False
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/live/calls/'+cid+'/live-audio',headers={'Origin':'https://evil.test'}): pass


def test_listening_receives_both_tracks_and_leaving_preserves_call(service,monkeypatch):
    client,manager,gateway=service;no_phone(monkeypatch,gateway)
    connection=FakeConnection();monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(connection))
    headers=owner_headers(client);cid=start(client,headers)['call_id']
    with client.websocket_connect('/live/browser-tests/'+cid+'/audio',headers=headers) as caller:
        caller.send_json({'event':'start','csrf':headers['X-CSRF-Token']});assert caller.receive_json()['event']=='ready'
        assert client.get('/live/calls/'+cid+'/live-audio',headers=headers).json()['available']
        with client.websocket_connect('/live/calls/'+cid+'/live-audio',headers=headers) as listener:
            listener.send_json({'event':'start','csrf':headers['X-CSRF-Token']});assert listener.receive_json()['event']=='ready'
            raw=bytes([180])*160
            caller.send_json({'event':'media','media':{'payload':base64.b64encode(raw).decode(),'timestamp':0}})
            assert listener.receive_json()['source']=='caller'
            assert listener.receive_json()['source']=='assistant'
            assert caller.receive_json()['event']=='media'
            assert caller.receive_json()['event']=='mark'
            listener.send_json({'event':'stop'})
            with pytest.raises(WebSocketDisconnect):listener.receive_json()
        assert manager.db.get(cid)['status']=='in_progress'
        caller.send_json({'event':'stop'})
    assert not manager.browser_audio.hubs


def test_bad_csrf_cannot_listen(service,monkeypatch):
    client,manager,_=service;connection=FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(connection))
    headers=owner_headers(client);cid=start(client,headers)['call_id']
    with client.websocket_connect('/live/browser-tests/'+cid+'/audio',headers=headers) as caller:
        caller.send_json({'event':'start','csrf':headers['X-CSRF-Token']});caller.receive_json()
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/live/calls/'+cid+'/live-audio',headers=headers) as listener:
                listener.send_json({'event':'start','csrf':'wrong'});listener.receive_json()
        assert not manager.browser_audio.current(cid).listeners
        caller.send_json({'event':'stop'})


def test_owner_asr_is_captioned_as_owner_and_delivered_as_context_not_caller_audio(monkeypatch):
    async def run():
        hub,_=make_hub();listener=Listener();queue=await hub.join(listener)
        captions=[];notes=[];hub.manager.db.human_caption=lambda *args:captions.append(args)
        async def append(**kwargs): notes.append(kwargs)
        caller_audio=AsyncMock();hub.bridge.connection.session=SimpleNamespace(
            instructions=SimpleNamespace(append=append),input_audio=SimpleNamespace(append=caller_audio))
        class ASR:
            async def __aenter__(self):return self
            async def __aexit__(self,*_):pass
            def __aiter__(self):return self
            async def __anext__(self):return await events.get()
        events=asyncio.Queue();asr=ASR()
        async def open_(*_):return asr
        monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(FakeConnection()))
        monkeypatch.setattr('app.calling.transcription.open_transcription',open_)
        monkeypatch.setattr('app.calling.transcription.configure',AsyncMock())
        ready=asyncio.Event();task=asyncio.create_task(transcribe_owner(hub,listener,queue,ready))
        await asyncio.wait_for(ready.wait(),1)
        await events.put(Event(type='conversation.item.input_audio_transcription.completed',item_id='one',transcript='Please let me explain.'))
        for _ in range(20):
            if notes:break
            await asyncio.sleep(.01)
        assert captions[0][2]=='owner'
        assert 'Please let me explain.' in notes[0]['content']
        assert 'never caller speech, caller consent' in notes[0]['content']
        caller_audio.assert_not_awaited()
        task.cancel();await asyncio.gather(task,return_exceptions=True);await hub.close()
    asyncio.run(run())


def test_missing_transcription_key_prevents_join_but_allows_listening():
    async def run():
        hub,_=make_hub();hub.manager.settings.openai_api_key=SimpleNamespace(get_secret_value=lambda:'')
        with pytest.raises(ValueError,match='transcription key'):await hub.join(Listener())
        assert hub.active() and hub.owner is None and not hub.paced
    asyncio.run(run())


def test_revoked_listener_closes_without_ending_call(service,monkeypatch):
    client,manager,_=service
    monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(FakeConnection()))
    headers=owner_headers(client);cid=start(client,headers)['call_id']
    listener_headers=owner_headers(client)
    with client.websocket_connect('/live/browser-tests/'+cid+'/audio',headers=headers) as caller:
        caller.send_json({'event':'start','csrf':headers['X-CSRF-Token']});caller.receive_json()
        with client.websocket_connect('/live/calls/'+cid+'/live-audio',headers=listener_headers) as listener:
            listener.send_json({'event':'start','csrf':listener_headers['X-CSRF-Token']});listener.receive_json()
            client.app.state.web_auth.logout(listener_headers['Cookie'].split('=',1)[1])
            with pytest.raises(WebSocketDisconnect):listener.receive_json()
        assert manager.db.get(cid)['status']=='in_progress'
        caller.send_json({'event':'stop'})


def test_join_routes_owner_to_recipient_only_and_leaving_keeps_stream(service,monkeypatch):
    client,manager,_=service;connection=FakeConnection()
    monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(connection))
    async def transcription(hub,listener,queue,ready):
        ready.set()
        try: await asyncio.Event().wait()
        finally: hub.leave(listener)
    monkeypatch.setattr('app.calling.browser_audio.transcribe_owner',transcription)
    headers=owner_headers(client);cid=start(client,headers)['call_id']
    with client.websocket_connect('/live/browser-tests/'+cid+'/audio',headers=headers) as caller:
        caller.send_json({'event':'start','csrf':headers['X-CSRF-Token']});caller.receive_json()
        with client.websocket_connect('/live/calls/'+cid+'/live-audio',headers=headers) as listener:
            listener.send_json({'event':'start','csrf':headers['X-CSRF-Token']});listener.receive_json()
            listener.send_json({'event':'join'})
            while listener.receive_json()['event']!='joined':pass
            assert caller.receive_json()['event']=='clear'
            payload=base64.b64encode(bytes([180])*160).decode()
            listener.send_json({'event':'media','payload':payload})
            assert caller.receive_json()['media']['payload']==payload
            assert not connection.audio, 'Owner must never contaminate caller input or booking evidence'
            listener.send_json({'event':'leave'})
            assert listener.receive_json()['event']=='left'
            assert not manager.browser_audio.current(cid).owner
            listener.send_json({'event':'stop'})
            with pytest.raises(WebSocketDisconnect):listener.receive_json()
        assert manager.db.get(cid)['status']=='in_progress'
        caller.send_json({'event':'stop'})
