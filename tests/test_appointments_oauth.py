import asyncio
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthClientInformationFull

from app.appointments.mcp_auth import SCOPES, TOOL_SCOPES
from app.config import Settings
from app.main import create_app
from app.mcp.events import VERSION
from tests.test_calls import MockTwilio


@pytest.fixture
def appointment_mcp(tmp_path):
    settings = Settings(_env_file=None, mcp_enabled=True, public_base_url='https://example.test',
        database_path=tmp_path/'calls.db', oauth_database_path=tmp_path/'oauth.db',
        appointments_enabled=True, appointment_database_path=tmp_path/'appointments.db',
        appointment_google_token_file=tmp_path/'unused-google.json',
        appointment_account_namespace='fixture-owner', appointment_mirror_status_url='http://localhost:9999')
    gateway = MockTwilio()
    app = create_app(settings, gateway=gateway)
    provider = app.state.oauth
    client = OAuthClientInformationFull(client_id='existing-client', client_name='Existing supervisor',
        redirect_uris=['https://chatgpt.com/callback'], token_endpoint_auth_method='none',
        grant_types=['authorization_code', 'refresh_token'], response_types=['code'],
        scope='phone.calls')
    asyncio.run(provider.register_client(client))
    tokens = provider._issue_tokens(client.client_id, ['phone.calls'], 'https://example.test/mcp')
    with TestClient(app) as http:
        with app.state.manager.db.connect() as db:
            db.execute('INSERT INTO mcp_event_subscriptions '
                '(id,owner,client_id,name,call_id,url,secret,expires_at,cursor) VALUES (?,?,?,?,?,?,?,?,?)',
                ('sub_fixture',json.dumps([client.client_id,'phone-agent-owner'],separators=(',',':')),
                 client.client_id,'call.incoming',None,'https://callback.example.com/events','fixture-secret',
                 time.time()+3600,0))
        def rpc(method, params=None, token=tokens.access_token):
            headers = {'Accept':'application/json, text/event-stream', 'MCP-Protocol-Version':VERSION,
                'MCP-Method':method, 'Authorization':'Bearer '+token}
            if method == 'tools/call':headers['MCP-Name'] = params['name']
            meta = {'io.modelcontextprotocol/protocolVersion':VERSION,
                'io.modelcontextprotocol/clientInfo':{'name':'fixture','version':'1'},
                'io.modelcontextprotocol/clientCapabilities':{}}
            return http.post('/mcp', headers=headers, json={'jsonrpc':'2.0','id':1,'method':method,
                'params':{**(params or {}),'_meta':meta}})
        yield app, http, rpc, tokens, client, gateway


def test_discovery_and_safe_permission_probe(appointment_mcp):
    app,http,rpc,tokens,client,gateway = appointment_mcp
    tools = {t['name']:t for t in rpc('tools/list').json()['result']['tools']}
    for name,scopes in TOOL_SCOPES.items():
        expected = [{'type':'oauth2','scopes':['phone.calls',*scopes]}]
        assert tools[name]['securitySchemes'] == expected
        assert tools[name]['_meta']['securitySchemes'] == expected
    result = rpc('tools/call', {'name':'get_appointment_permissions','arguments':{}}).json()['result']
    assert result['isError'] and 'mcp/www_authenticate' in result['_meta']
    challenge = result['_meta']['mcp/www_authenticate'][0]
    assert 'error="insufficient_scope"' in challenge and 'error_description=' in challenge
    for scope in ['phone.calls',*SCOPES]:assert scope in challenge
    url = challenge.split('resource_metadata="')[1].split('"')[0]
    metadata = http.get(url).json()
    assert metadata['resource'] == 'https://example.test/mcp'
    assert set(metadata['scopes_supported']) == {'phone.calls',*SCOPES}
    assert not gateway.calls
    with app.state.manager.appointments.store.connect() as db:
        assert db.execute('SELECT count(*) FROM booking_operations').fetchone()[0] == 0
    assert rpc('tools/call', {'name':'get_voice_settings','arguments':{}}).json()['result']['isError'] is False


@pytest.mark.parametrize('name', [n for n in TOOL_SCOPES if n != 'get_appointment_permissions'])
def test_missing_scope_stops_before_any_business_action(appointment_mcp,name):
    app,http,rpc,tokens,client,gateway = appointment_mcp
    # No call/appointment inputs are required to request consent; nothing can execute yet.
    result = rpc('tools/call', {'name':name,'arguments':{}}).json()['result']
    assert result['isError'] and 'mcp/www_authenticate' in result['_meta']
    expected = 'scope="'+' '.join(['phone.calls',*TOOL_SCOPES[name]])+'"'
    assert expected in result['_meta']['mcp/www_authenticate'][0]
    assert not gateway.calls


def test_incremental_challenge_preserves_existing_appointment_scopes(appointment_mcp):
    app,http,rpc,tokens,client,gateway = appointment_mcp
    partial = app.state.oauth._issue_tokens(client.client_id, ['phone.calls',SCOPES[0]], 'https://example.test/mcp')
    result = rpc('tools/call', {'name':'book_appointment','arguments':{}}, partial.access_token).json()['result']
    assert f'scope="phone.calls {SCOPES[0]} {SCOPES[1]}"' in result['_meta']['mcp/www_authenticate'][0]
    assert SCOPES[2] not in result['_meta']['mcp/www_authenticate'][0]


def test_approval_pkce_reuses_client_and_preserves_existing_connection(appointment_mcp):
    app,http,rpc,tokens,client,gateway = appointment_mcp
    provider = app.state.oauth
    verifier = 'x'*48
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    scopes = ['phone.calls',*SCOPES]
    before = asyncio.run(provider.load_access_token(tokens.access_token))
    with provider._connect() as db:
        client_before = db.execute('SELECT payload FROM oauth_clients WHERE client_id=?',(client.client_id,)).fetchone()[0]
    with app.state.manager.db.connect() as db:
        subscriptions_before = [tuple(r) for r in db.execute('SELECT * FROM mcp_event_subscriptions')]
    response = http.get('/authorize', params={'client_id':client.client_id,'response_type':'code',
        'redirect_uri':'https://chatgpt.com/callback','scope':' '.join(scopes),'state':'fixture-state',
        'code_challenge':challenge,'code_challenge_method':'S256','resource':'https://example.test/mcp'},
        follow_redirects=False)
    assert response.status_code == 302, response.text
    page = http.get(response.headers['location'])
    for scope in SCOPES:assert scope in page.text
    request = parse_qs(urlsplit(response.headers['location']).query)['request'][0]
    denied = http.post('/approve', data={'request':request,'code':'invalid'}, follow_redirects=False)
    assert 'invalid, expired, or already used' in denied.text
    assert provider.approval_details(request) is not None
    code = provider.create_pairing_code()
    accepted = http.post('/approve', data={'request':request,'code':code}, follow_redirects=False)
    assert accepted.status_code == 302
    query = parse_qs(urlsplit(accepted.headers['location']).query)
    assert query['state'] == ['fixture-state']
    auth_code = query['code'][0]
    response = http.post('/token', data={'grant_type':'authorization_code','code':auth_code,
        'redirect_uri':'https://chatgpt.com/callback','client_id':client.client_id,'code_verifier':verifier,
        'resource':'https://example.test/mcp'})
    assert response.status_code == 200, response.text
    upgraded = response.json()
    assert set(upgraded['scope'].split()) == set(scopes)
    access = asyncio.run(provider.load_access_token(upgraded['access_token']))
    assert access.client_id == before.client_id and access.subject == before.subject
    result = rpc('tools/call', {'name':'get_appointment_permissions','arguments':{}}, upgraded['access_token']).json()['result']
    value = result.get('structuredContent') or json.loads(result['content'][0]['text'])
    assert not result['isError'] and value['status'] == 'authorized'
    assert value['appointment_action_performed'] is False
    assert asyncio.run(provider.load_access_token(tokens.access_token)) == before
    with provider._connect() as db:
        assert db.execute('SELECT payload FROM oauth_clients WHERE client_id=?',(client.client_id,)).fetchone()[0] == client_before
    assert not provider._consume_pairing_code(code) and not gateway.calls
    with app.state.manager.db.connect() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM mcp_event_subscriptions')] == subscriptions_before


def test_refresh_cannot_grant_additional_permissions(appointment_mcp):
    app,http,rpc,tokens,client,gateway = appointment_mcp
    response = http.post('/token', data={'grant_type':'refresh_token','refresh_token':tokens.refresh_token,
        'client_id':client.client_id,'scope':' '.join(['phone.calls',*SCOPES]),
        'resource':'https://example.test/mcp'})
    assert response.status_code == 400
    with app.state.oauth._connect() as db:
        for row in db.execute('SELECT payload FROM oauth_access_tokens'):
            assert json.loads(row[0])['scopes'] == ['phone.calls']
    assert rpc('tools/call', {'name':'get_appointment_permissions','arguments':{}}).json()['result']['isError']
