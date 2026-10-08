import json
import sqlite3

from fastapi.testclient import TestClient

from app.mcp.callback import signed_headers
from scripts.mcp_webhook_receiver import app
from tests.test_mcp_events import SECRET


def test_receiver_challenge_delivery_deduplication_and_tamper(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('MCP_WEBHOOK_SECRET', SECRET)
    with TestClient(app) as client:
        body = b'{"type":"verification","challenge":"synthetic-test-challenge"}'
        headers = signed_headers('sub-test','msg-test',SECRET,body)
        response = client.post('/events',content=body,headers=headers)
        assert response.json()=={'challenge':'synthetic-test-challenge'}
        event = {'eventId':'evt-test','name':'transcript.delta','timestamp':'2026-10-01T12:00:00Z',
                 'data':{'text':'Synthetic test'},'cursor':None}
        body = json.dumps(event).encode()
        headers = signed_headers('sub-test','evt-test',SECRET,body)
        assert client.post('/events',content=body,headers=headers).status_code==204
        assert client.post('/events',content=body,headers=headers).status_code==204
        assert client.post('/events',content=body+b' ',headers=headers).status_code==401
        assert client.post('/events',content=body,headers={**headers,'webhook-id':'wrong'}).status_code==401
        assert client.post('/events',content=body).status_code==401
    with sqlite3.connect(tmp_path/'data/received-mcp-events.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM received').fetchone()[0]==1
