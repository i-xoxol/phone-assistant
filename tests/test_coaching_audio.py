import asyncio
import base64

import pytest
from starlette.websockets import WebSocketDisconnect

from app.calling.coaching_audio import NativeTurns,linear_mulaw
from app.calling.live_session import mulaw_sample
from tests.test_calls import service
from tests.test_inbound import SID
from tests.test_live import stream_headers
from tests.test_owner_realtime import ready
from tests.test_owner_transfer import CHILD


def test_native_turns_endpoint_without_any_transcription_and_bound_long_audio():
    turns=NativeTurns()
    for _ in range(10):assert not turns.feed(b'\xff'*160)
    for _ in range(5):assert not turns.feed(b'\x00'*160)
    results=[]
    for _ in range(22):results.extend(turns.feed(b'\xff'*160))
    assert len(results)==1 and results[0][1] is True and b'\x00'*160 in results[0][0]
    assert len(results[0][0])<8000
    continuous=NativeTurns();pieces=[]
    for _ in range(405):pieces.extend(continuous.feed(b'\x00'*160))
    assert len(pieces)==1 and len(pieces[0][0])==64000 and pieces[0][1] is False


@pytest.mark.parametrize('value',[-30000,-10000,-1000,0,1000,10000,30000])
def test_pcm_mulaw_encoder_preserves_polarity_and_level(value):
    decoded=mulaw_sample(linear_mulaw(value))
    assert abs(decoded-value)<=max(8,abs(value)*.04)


@pytest.mark.parametrize('side,sid',[('owner',CHILD),('caller',SID)])
def test_direct_microphone_coaching_survives_caption_provider_failure_and_keeps_source_identity(service,monkeypatch,side,sid):
    client,m,g,cid,tid=ready(service)
    observed=[];original=m.observer.realtime.audio_turn
    def capture(tid,side,raw,complete=True):
        observed.append((side,raw,complete));original(tid,side,raw,complete)
    m.observer.realtime.audio_turn=capture
    def unavailable(_):raise RuntimeError('private key must not leak')
    monkeypatch.setattr('app.calling.coaching_audio.live_client',unavailable)
    path='/twilio/owner-media/'+tid+'/'+side
    with client.websocket_connect(path,headers=stream_headers(path,'wss')) as ws:
        ws.send_json({'event':'start','start':{'accountSid':'AC123','callSid':sid,'streamSid':'MZ_NATIVE',
            'tracks':['inbound'],'mediaFormat':{'encoding':'audio/x-mulaw','sampleRate':8000,'channels':1}}})
        for raw in [b'\x00'*160]*6+[b'\xff'*160]*22:
            ws.send_json({'event':'media','streamSid':'MZ_NATIVE','media':{'track':'inbound',
                'payload':base64.b64encode(raw).decode()}})
        ws.send_json({'event':'stop','streamSid':'MZ_NATIVE'})
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    assert observed and all(item[0]==side for item in observed)
    assert any(b'\x00'*160 in item[1] and item[2] for item in observed)
    assert m.transfer.get(tid)['transcription_state']=='degraded'
    assert m.db.get(cid)['status']=='in_progress'
    assert 'private key' not in str(m.result(cid))
