import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings
from app.logging_config import log_event
from app.storage.database import Database
from app.storage.models import CallRequest, DigitsRequest, CallUpdateRequest, TERMINAL
from app.events import EventHub


def normalize_status(status: str) -> str:
    return {'initiated': 'dialing', 'in-progress': 'in_progress',
            'no-answer': 'no_answer', 'canceled': 'cancelled'}.get(status, status)


class CallManager:
    def __init__(self, settings: Settings, database: Database, gateway):
        self.settings, self.db, self.gateway = settings, database, gateway
        self.bridges = {}
        self.navigation = {}
        self.events = EventHub()
        self.db.on_event = self.events.notify
        self.tool_tasks = set()
        self.stopping = set()

    def attach_bridge(self, call_id, bridge):
        if call_id in self.stopping:
            raise ValueError('Call is being stopped by the owner')
        if previous := self.bridges.get(call_id):
            if not previous.handoff:
                raise ValueError('A voice stream already owns this call')
        self.bridges[call_id] = bridge
        if ready := self.navigation.get(call_id):
            ready.set()

    def detach_bridge(self, call_id, bridge):
        if self.bridges.get(call_id) is bridge:
            self.bridges.pop(call_id, None)

    async def send_call_update(self, call_id: str, request: CallUpdateRequest) -> dict:
        self.db.get(call_id)
        update_id = str(request.update_id or uuid4())
        existing = self.db.call_update(call_id, update_id)
        bridge = self.bridges.get(call_id)
        if not existing and (call_id in self.stopping or
                (bridge and bridge.end_requested.is_set() and not bridge.handoff)):
            raise ValueError('Call is ending; an update cannot reverse hangup')
        row = self.db.queue_update(call_id, update_id, request.content, request.mode)
        if bridge and not bridge.closing and not bridge.handoff:
            bridge.updates_ready.set()
            await bridge.send_pending_updates()
            row = self.db.call_update(call_id, update_id)
        log_event('call_context_update', call_id=call_id, update_id=update_id, mode=row['mode'], status=row['status'])
        return {**row, 'delivery_note': 'Acknowledged means context reached GPT-Live, not spoken or acted on. '
            'Use the transcript to verify behavior. Retry only with this same update_id.'}

    async def press_digits(self, call_id: str, request: DigitsRequest) -> dict:
        if call_id in self.stopping:
            raise ValueError('Call is being stopped by the owner')
        record = self.db.get(call_id)
        bridge = self.bridges.get(call_id)
        if record['status'] != 'in_progress' or not bridge or bridge.closing:
            raise ValueError('Keypad requires a connected voice stream')
        if call_id in self.navigation or bridge.end_requested.is_set():
            raise ValueError('Call is already changing streams or ending')
        ready = self.navigation[call_id] = asyncio.Event()
        # Set BEFORE redirect: intentional Stream closure must not trigger hangup.
        bridge.handoff = True
        bridge.playback_sealed = True
        try:
            await self.gateway.press_digits(record['twilio_call_sid'], call_id, request.digits)
            self.db.keypad(call_id, request.digits, request.reason, 'submitted')
            log_event('keypad_submitted', call_id=call_id, digit_count=len(request.digits))
            await asyncio.wait_for(ready.wait(), timeout=25)
            return {**self.status(call_id), 'keypad_status': 'submitted_and_reconnected'}
        except Exception:
            self.db.keypad(call_id, request.digits, request.reason, 'unconfirmed')
            await self.hangup_call(call_id)
            raise RuntimeError('Keypad operation unconfirmed; call ended. Do not retry automatically.') from None
        finally:
            self.navigation.pop(call_id, None)

    async def place_call(self, request: CallRequest) -> dict:
        if missing := self.settings.missing():
            raise ValueError('Missing configuration: ' + ', '.join(missing))
        call_id = self.db.create(request)
        log_event('call_created', call_id=call_id)
        try:
            call = await self.gateway.place(call_id, request.phone_number, request.max_duration_minutes)
            self.db.update(call_id, twilio_call_sid=call.sid)
            self.transition(call_id, normalize_status(call.status))
        except Exception as exc:
            # Do not store SDK messages, which can include URLs/numbers/credentials.
            self.db.update(call_id, error_message=f'Twilio creation failed or unconfirmed ({type(exc).__name__}); check Twilio before retrying.')
            self.transition(call_id, 'failed')
            log_event('twilio_create_failed', call_id=call_id, error_type=type(exc).__name__)
        return self.status(call_id)

    def transition(self, call_id: str, status: str, **kwargs):
        if self.db.transition(call_id, status, **kwargs):
            log_event('call_state_changed', call_id=call_id, status=status)

    def status(self, call_id: str) -> dict:
        record = self.db.get(call_id)
        duration = record['duration_seconds']
        if record['started_at'] and record['status'] not in TERMINAL:
            duration = int((datetime.now(timezone.utc) - datetime.fromisoformat(record['started_at'])).total_seconds())
        return {'call_id': call_id, 'status': record['status'], 'duration_seconds': max(0, duration),
                'voice_tools_enabled': self.settings.live_tools_enabled,
                'call_updates_supported': True,
                'voice_control_mode': 'live_responses_backend' if self.settings.live_tools_enabled else 'external_mcp_client',
                'voice_control_note': ('The live voice backend handles spoken keypad requests directly. '
                    'Do not wait for ChatGPT polling or send duplicate digits.' if self.settings.live_tools_enabled else
                    'The originating MCP client must send keypad actions.'),
                'live_url': self.settings.public_url(f'/live?call_id={call_id}')}

    async def refresh(self, call_id: str) -> dict:
        record = self.db.get(call_id)
        if record['twilio_call_sid']:
            call = await self.gateway.fetch(record['twilio_call_sid'])
            self.transition(call_id, normalize_status(call.status),
                            duration=int(call.duration) if call.duration is not None else None)
        return self.status(call_id)

    async def hangup_call(self, call_id: str) -> dict:
        record = self.db.get(call_id)
        self.stopping.add(call_id)
        # Mute before any provider network I/O. Works even if caption delivery fails.
        bridge = self.bridges.get(call_id)
        if bridge:
            bridge.playback_sealed = True
            if getattr(bridge, 'ws', None):
                try:
                    await asyncio.wait_for(bridge.ws.send_json({'event': 'clear', 'streamSid': bridge.stream_sid}), 0.5)
                except Exception:
                    pass
        if record['status'] not in TERMINAL and record['twilio_call_sid']:
            self.db.emit(call_id, 'call.hangup_requested', {'audio_muted': bool(bridge)})
            if hasattr(self.gateway, 'hangup_fast'):
                call = await self.gateway.hangup_fast(record['twilio_call_sid'], record['status'])
            else:
                call = await self.gateway.hangup(record['twilio_call_sid'])
            self.transition(call_id, normalize_status(call.status))
        return self.status(call_id)
