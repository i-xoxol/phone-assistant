import asyncio,base64,time
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from app.calling.owner_audio_bridge import Leg,FRAME,SILENCE
from app.calling.owner_realtime import facts_key
from tests.test_calls import service
from tests.test_owner_audio_bridge import setup
from tests.test_owner_observation import cb
from tests.test_owner_speech import speech_ready
from tests.test_owner_realtime import ready


def test_partial_bridge_times_out_even_when_model_is_not_ready(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch)
    monkeypatch.setattr('app.calling.owner_audio_bridge.PEER_ATTACH_SECONDS',.02)
    m.transfer.update(tid,accepted=1,mode='whisper',status='connected')
    room=m.observer.audio_bridge.rooms[tid]
    class Socket:
        async def send_json(self,data):pass
        async def close(self):pass
    room.legs={'caller':Leg(Socket(),'MZ_PARTIAL')}
    client.portal.call(room.watch_peer)
    assert m.observer.audio_bridge.in_fallback(tid)
    assert m.transfer.get(tid)['mode']=='transcript' and m.db.get(cid)['status']=='in_progress'
    assert g.rings==1 and tid not in m.observer.audio_bridge.rooms


def test_caption_reader_normal_exit_marks_degraded_but_native_audio_continues(service,monkeypatch):
    from app.calling.coaching_audio import capture_with_coaching
    client,m,g,cid,tid=ready(service);observed=[]
    class Connection:
        def __aiter__(self):return self
        async def __anext__(self):raise StopAsyncIteration
    @asynccontextmanager
    async def opened(*args):yield Connection()
    @asynccontextmanager
    async def client_factory(*args):yield object()
    async def open_connection(*args):return opened()
    async def configure(*args):pass
    monkeypatch.setattr('app.calling.coaching_audio.live_client',client_factory)
    monkeypatch.setattr('app.calling.transcription.open_transcription',open_connection)
    monkeypatch.setattr('app.calling.transcription.configure',configure)
    m.observer.realtime.audio_turn=lambda *args,**kwargs:observed.append(args)
    class Socket:
        app=SimpleNamespace(state=SimpleNamespace(manager=m))
        def __init__(self):self.frames=iter([b'\x00'*160]*6+[b'\xff'*160]*22)
        async def receive_json(self):
            await asyncio.sleep(.002)
            raw=next(self.frames,None)
            return {'event':'stop','streamSid':'MZ_TEST'} if raw is None else {
                'event':'media','streamSid':'MZ_TEST','media':{'track':'inbound','payload':base64.b64encode(raw).decode()}}
    client.portal.call(capture_with_coaching,Socket(),m.transfer.get(tid),'caller','MZ_TEST',0)
    assert observed and m.transfer.get(tid)['transcription_state']=='degraded'
    assert m.db.get(cid)['status']=='in_progress' and g.rings==1


def test_paused_tts_revokes_queued_callback_and_audio_fetch(service,monkeypatch):
    client,m,g,cid,tid,note=speech_ready(service,monkeypatch)
    m.observer.requests[tid]={'turn_id':'q1','text':'Assistant, my Monday schedule?'}
    asyncio.run(m.observer.coach(tid));action=g.whispers[0][2].rsplit('/',1)[1]
    wid=action.removeprefix('whisper-')
    def pause():m.observer.transcript_changed(tid,'owner','Assistant, stop coaching','pause-1')
    client.portal.call(pause)
    assert '<Play' not in cb(client,m,tid,action).text
    with pytest.raises(KeyError):m.observer.audio_for(tid,wid)
    with m.db.connect() as db:assert db.execute('SELECT state FROM owner_whispers WHERE whisper_id=?',(wid,)).fetchone()[0]=='cancelled'
    assert m.db.get(cid)['status']=='in_progress'


def test_twenty_ms_mixer_does_not_open_database_even_when_database_fails(service,monkeypatch):
    client,m,g,cid,tid=setup(service,monkeypatch);cb(client,m,tid,'accept',Digits='1')
    pool=m.observer.realtime;ch=pool.channels[tid];room=m.observer.audio_bridge.rooms[tid]
    snapshot=facts_key([]);ch.connected.set();ch.delivery_permission=(ch.generation,snapshot);ch.delivery_until=time.monotonic()+5
    class Socket:
        def __init__(self):self.packets=[]
        async def send_json(self,data):self.packets.append(data)
    caller=Socket();owner=Socket();room.legs={'caller':Leg(caller,'MZ_CALLER'),'owner':Leg(owner,'MZ_OWNER')}
    async def exercise():
        original=m.db.connect;attempts=[]
        def fail(*a,**k):attempts.append(True);raise OSError('synthetic database outage')
        m.db.connect=fail
        try:
            for _ in range(100):
                await ch.queue.put((ch.generation,snapshot,b'\x80'*FRAME))
                hint,marks=room.hint_frame()
                await room.send('caller',b'\x20'*FRAME,hint)
                await room.send('owner',SILENCE,hint)
            assert not attempts
        finally:m.db.connect=original
    client.portal.call(exercise)
    assert len(caller.packets)==100 and len(owner.packets)==100
    assert all(base64.b64decode(p['media']['payload'])==b'\x20'*FRAME for p in caller.packets)
