"""Verify remote authenticated MCP retrieval without placing calls; run on the service host."""
import asyncio
import base64
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx2 as httpx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings
from app.oauth_provider import PairingOAuthProvider
from app.storage.database import Database


async def check():
    settings = Settings()
    provider = PairingOAuthProvider(settings.oauth_database_path, settings.public_base_url)
    resource = settings.public_base_url + '/mcp'
    tokens = provider._issue_tokens('local-diagnostic', ['phone.calls'], resource)
    try:
        async with httpx.AsyncClient(headers={'Authorization': 'Bearer ' + tokens.access_token}, timeout=30) as http:
            async def rpc(method, params=None):
                meta = {'io.modelcontextprotocol/protocolVersion':'2026-07-28',
                    'io.modelcontextprotocol/clientInfo':{'name':'phone-diagnostic','version':'1'},
                    'io.modelcontextprotocol/clientCapabilities':{}}
                response = await http.post(resource, headers={'Accept':'application/json, text/event-stream',
                    'MCP-Protocol-Version':'2026-07-28','MCP-Method':method},
                    json={'jsonrpc':'2.0','id':1,'method':method,'params':{**(params or {}),'_meta':meta}})
                response.raise_for_status()
                return response.json()
            discover = await rpc('server/discover')
            assert discover['result']['capabilities']['events'] == {}
            catalog = await rpc('events/list')
            assert {e['name'] for e in catalog['result']['events']} == {'transcript.delta','call.status'}
            print(json.dumps({'mcp_events_discoverable':True,'protocol':'2026-07-28'}))
            for mode in ('auto', 'legacy'):
                async with Client(streamable_http_client(resource, http_client=http), mode=mode) as session:
                    names = [t.name for t in (await session.list_tools()).tools]
                    assert set(names) == {'place_call','get_call_status','get_call_result','hangup_call','press_digits',
                        'subscribe_call_events','get_call_event_subscriptions','unsubscribe_call_events','send_call_update'}
                    db = Database(settings.database_path)
                    with db.connect() as conn:
                        row = conn.execute('SELECT call_id FROM calls ORDER BY created_at DESC LIMIT 1').fetchone()
                    if row:
                        status = await session.call_tool('get_call_status', {'call_id':row['call_id']})
                        result = await session.call_tool('get_call_result', {'call_id':row['call_id']})
                        assert not status.is_error and not result.is_error
                        record = json.loads(result.content[0].text)
                        assert record['call_id'] == row['call_id']
                        assert 'voice_tool_operations' in record
                        assert 'call_updates' in record and record['call_updates_supported']
                        subscription_result = await session.call_tool('get_call_event_subscriptions', {'call_id':row['call_id']})
                        assert not subscription_result.is_error
                        subscription_state=json.loads(subscription_result.content[0].text)
                        assert subscription_state['native_mcp_events_supported']
                    print(json.dumps({'authenticated_mcp':True,'mode':mode,'tools':names,'retrieval_verified':bool(row),
                        'voice_tools_enabled': record['voice_tools_enabled'] if row else None,'dialed_phone':False}))
            if row:
                blocked = await rpc('events/subscribe', {'name':'transcript.delta',
                    'arguments':{'call_id':row['call_id']}, 'delivery':{'mode':'webhook',
                    'url':'https://127.0.0.1/private',
                    'secret':'whsec_'+base64.b64encode(b'diagnostic-key-do-not-use-in-prod').decode()}})
                assert blocked['error']['code'] == -32015
                assert blocked['error']['data']['reason'] == 'blocked_address'
                print(json.dumps({'callback_ssrf_block_verified':True,'transcript_exported':False}))
    finally:
        await provider.revoke_token(await provider.load_access_token(tokens.access_token))
        refresh = await provider.load_refresh_token(None, tokens.refresh_token)
        if refresh:
            await provider.revoke_token(refresh)


if __name__ == '__main__':
    asyncio.run(check())
