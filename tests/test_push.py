import asyncio
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.api.live import COOKIE, stream_events
from app.events import WebhookDispatcher, signature
from app.storage.database import Database
from tests.test_calls import service, create


def test_pushed_delta_replay_without_polling(service):
    client, manager, _ = service
    cid = create(client).json()['call_id']
    async def exercise():
        head = manager.db.event_head()
        stream = stream_events(manager, head, time.time()+60)
        assert 'connected' in await anext(stream)
        waiting = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        start = time.monotonic()
        event = {'event_id':'push1','type':'session.input_transcript.delta',
                 'start_ms':10,'end_ms':100,'delta':'Live text'}
        manager.db.transcript(cid,event)
        chunk = await asyncio.wait_for(waiting, .5)
        assert time.monotonic()-start < .5
        assert 'transcript.delta' in chunk and 'Live text' in chunk
        manager.db.transcript(cid,event)
        assert len(manager.db.events_after(head)) == 1
        await stream.aclose()
        replay = Database(manager.db.path).events_after(head)
        assert replay[0]['data']['speaker'] == 'callee'
    asyncio.run(exercise())


def test_browser_pairing_csrf_and_direct_hangup(service):
    client, manager, gateway = service
    client.base_url = 'https://example.test'
    assert client.get('/live/bootstrap').status_code == 401
    assert client.get('/live/events').status_code == 401
    code = client.app.state.web_auth.create_code()
    assert client.post('/live/login',data={'code':code}).status_code == 403
    result = client.post('/live/login',data={'code':code},headers={'Origin':'https://example.test'})
    assert result.status_code == 200
    cookie = client.cookies.get(COOKIE)
    assert cookie
    assert client.app.state.web_auth.login(code) is None
    page = client.get('/live')
    assert 'Hang up call' in page.text and "script-src 'self'" in page.headers['content-security-policy']
    cid = create(client).json()['call_id']
    path = f'/live/calls/{cid}/hangup'
    assert client.post(path).status_code == 403
    csrf = client.app.state.web_auth.session(cookie)['csrf']
    headers={'Origin':'https://example.test','X-CSRF-Token':csrf}
    assert client.post(path,headers={**headers,'Origin':'https://evil.test'}).status_code == 403
    assert client.post(path,headers=headers).json()['status'] == 'cancelled'
    assert gateway.status == 'canceled'
    assert client.post('/live/logout',headers=headers).status_code == 204
    assert client.get('/live/bootstrap').status_code == 401


def test_mute_precedes_provider_disconnect_and_failure_is_visible(service):
    client, manager, gateway = service
    cid = create(client).json()['call_id']
    manager.transition(cid,'in_progress')
    messages = []
    class Socket:
        async def send_json(self,message): messages.append(message)
    bridge=SimpleNamespace(playback_sealed=False,ws=Socket(),stream_sid='MZ_TEST')
    manager.bridges[cid]=bridge
    async def fail(sid):
        assert bridge.playback_sealed
        assert messages[0]['event'] == 'clear'
        raise RuntimeError('private provider detail')
    gateway.hangup=fail
    result=client.post(f'/calls/{cid}/hangup')
    assert result.status_code == 502 and 'private provider detail' not in result.text
    assert manager.status(cid)['status'] == 'in_progress'
    assert cid in manager.stopping
    with pytest.raises(ValueError,match='stopped'):
        manager.attach_bridge(cid,SimpleNamespace(handoff=False))


def test_webhook_signed_ordered_and_durable_retry(service):
    client, manager, _ = service
    manager.settings.webhook_url='https://receiver.test/events'
    # Test uses SecretStr just like loaded settings.
    from pydantic import SecretStr
    manager.settings.webhook_secret=SecretStr('test-signing-key')
    dispatcher=WebhookDispatcher(manager)
    cursor=manager.db.webhook_cursor(dispatcher.destination)
    cid=create(client).json()['call_id']
    event=manager.db.events_after(cursor)[0]
    attempts=[]
    def receiver(request):
        stamp=request.headers['X-Phone-Timestamp']
        expected='sha256='+hmac.new(b'test-signing-key',stamp.encode()+b'.'+request.content,hashlib.sha256).hexdigest()
        assert hmac.compare_digest(expected,request.headers['X-Phone-Signature'])
        assert json.loads(request.content)['call_id']==cid
        attempts.append(request.headers['X-Phone-Event-Id'])
        return httpx.Response(503 if len(attempts)==1 else 204)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(receiver)) as session:
            with pytest.raises(httpx.HTTPStatusError): await dispatcher.deliver(session,event)
            assert manager.db.webhook_cursor(dispatcher.destination)==cursor
            await dispatcher.deliver(session,event)
        assert Database(manager.db.path).webhook_cursor(dispatcher.destination)==event['id']
    asyncio.run(exercise())
    assert attempts[0]==attempts[1]


def test_browser_code_expiry_and_secure_cookie(service):
    client, manager, _ = service
    auth=client.app.state.web_auth
    code=auth.create_code(minutes=-1)
    assert auth.login(code) is None
    client.base_url='https://example.test'
    code=auth.create_code()
    response=client.post('/live/login',data={'code':code},headers={'Origin':'https://example.test'},follow_redirects=False)
    cookie=response.headers['set-cookie']
    assert 'Secure' in cookie and 'HttpOnly' in cookie and 'SameSite=strict' in cookie


def test_status_webhook_event_matches_persisted_terminal_state(service):
    client, manager, _ = service
    cid=create(client).json()['call_id']
    manager.transition(cid,'no_answer')
    event=manager.db.events_after(0)[-1]
    assert event['type']=='call.status' and event['data']['status']=='no_answer'
    assert manager.db.get(cid)['status']=='no_answer'


def test_example_receiver_authentication_and_deduplication(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scripts.webhook_receiver import app
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('WEBHOOK_SECRET','receiver-test')
    body=b'{"id":1,"type":"transcript.delta","data":{"text":"test"}}'
    stamp=str(int(time.time()))
    headers={'X-Phone-Timestamp':stamp,'X-Phone-Event-Id':'1',
             'X-Phone-Signature':signature('receiver-test',stamp,body)}
    with TestClient(app) as client:
        assert client.post('/events',content=body,headers=headers).status_code==204
        assert client.post('/events',content=body,headers=headers).status_code==204
        assert client.post('/events',content=body+b' ',headers=headers).status_code==401
        old=str(int(time.time())-600)
        assert client.post('/events',content=body,headers={**headers,'X-Phone-Timestamp':old,
            'X-Phone-Signature':signature('receiver-test',old,body)}).status_code==401
    import sqlite3
    with sqlite3.connect(tmp_path/'data/received-events.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM received').fetchone()[0]==1


def test_pair_mobile_requires_write_auth(service):
    client, manager, _ = service
    client.base_url='https://example.test'
    code=client.app.state.web_auth.create_code()
    client.post('/live/login',data={'code':code},headers={'Origin':'https://example.test','Accept':'application/json'})
    assert client.post('/live/pairing-code').status_code==403
    csrf=client.app.state.web_auth.session(client.cookies.get(COOKIE))['csrf']
    result=client.post('/live/pairing-code',headers={'Origin':'https://example.test','X-CSRF-Token':csrf})
    assert result.status_code==200 and result.json()['expires_in_seconds']==900
    assert client.app.state.web_auth.login(result.json()['code'])
