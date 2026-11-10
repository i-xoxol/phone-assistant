"""Credential-free local demo. Real dialing is disabled; provider keys are invalid."""
from types import SimpleNamespace
from uuid import UUID

from fastapi import Depends
from app.api.calls import authenticated
from app.config import Settings
from app.main import create_app


class DemoGateway:
    async def place(self, call_id, *args, **kwargs):
        return SimpleNamespace(sid='DEMO_'+call_id, status='queued')
    async def fetch(self, sid):
        return SimpleNamespace(sid=sid, status='queued', duration='0')
    async def hangup(self, sid):
        return SimpleNamespace(sid=sid, status='canceled', duration='0')
    async def verified_caller_id(self, number):
        return False
    def __getattr__(self, name):
        raise RuntimeError('Telephone access is disabled in the local managed demo')


def build_demo():
    settings = Settings(_env_file=None, openai_api_key='demo-not-a-key',
        elevenlabs_api_key='', gemini_api_key='', voice_provider='openai',
        twilio_account_sid='DEMO', twilio_auth_token='demo-not-a-token',
        twilio_phone_number='+12025550100', inbound_enabled=False,
        subscription_lookup_enabled=False, call_recording_enabled=False,
        owner_transfer_enabled=False, owner_observation_enabled=False,
        appointments_enabled=False, owner_calendar_coaching_enabled=False)
    application = create_app(settings, gateway=DemoGateway())

    @application.post('/demo/complete/{call_id}', dependencies=[Depends(authenticated)])
    def complete(call_id: UUID):
        db = application.state.manager.db
        cid = str(call_id)
        db.transition(cid, 'in_progress')
        db.transcript(cid, {'speaker':'assistant', 'event_id':'demo-assistant', 'start_ms':0, 'end_ms':1000,
            'delta':'This is a simulated call. No telephone was dialed.'})
        db.transcript(cid, {'speaker':'callee', 'event_id':'demo-callee', 'start_ms':1000, 'end_ms':2000,
            'delta':'The local PostgreSQL demo works.'})
        db.transition(cid, 'completed')
        return {'call_id':cid, 'status':'completed', 'simulated':True}

    # The MCP catch-all mount must remain after this demo-only endpoint.
    application.router.routes.insert(0, application.router.routes.pop())

    # Block HTTP AND WebSocket provider endpoints before application routing.
    async def isolated(scope, receive, send):
        path = scope.get('path', '')
        if path.startswith(('/twilio', '/browser-tests', '/browser-audio')):
            if scope['type'] == 'websocket':
                await send({'type':'websocket.close', 'code':1008})
            else:
                await send({'type':'http.response.start', 'status':403, 'headers':[]})
                await send({'type':'http.response.body', 'body':b'Local demo: providers disabled'})
            return
        await application(scope, receive, send)
    return isolated
