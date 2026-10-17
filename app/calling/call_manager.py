import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

from app.config import Settings
from app.logging_config import log_event
from app.storage.database import Database
from app.storage.models import CallRequest, DigitsRequest, CallUpdateRequest, SupervisorReply, TERMINAL
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
        self.supervisor_tasks = {}

    async def request_supervisor(self, call_id: str, question: str) -> dict:
        if call_id in self.stopping:
            raise ValueError('Call is stopping')
        expires = (datetime.now(timezone.utc)+timedelta(seconds=self.settings.supervisor_wait_seconds)).isoformat()
        request = self.db.create_supervisor_request(call_id, question.strip(), expires)
        bridge = getattr(self, 'mcp_events', None)
        available = bool(bridge and bridge.ready_for('call.supervisor_requested'))
        if request['request_id'] not in self.supervisor_tasks:
            task = asyncio.create_task(self.wait_for_supervisor(request['request_id']))
            self.supervisor_tasks[request['request_id']] = task
            task.add_done_callback(lambda _t: self.supervisor_tasks.pop(request['request_id'], None))
        return {**request, 'booking_allowed':bool(self.db.get(call_id)['inbound_booking_allowed']),
            'live_supervisor_available':available,
            'instruction': ('Request saved for the verified supervisor connection. Keep listening; do not invent an answer. '
                'Reply arrives as an owner update. If delayed, take a message.' if available else
                'No active live supervisor subscription. Do not promise a live lookup or booking. Take a message for follow-up.')}

    async def wait_for_supervisor(self, request_id: str):
        request = self.db.supervisor_request(request_id)
        delay = max(0, (datetime.fromisoformat(request['expires_at'])-datetime.now(timezone.utc)).total_seconds())
        await asyncio.sleep(delay)
        request = self.db.supervisor_request(request_id)
        if request['status'] != 'pending':
            return
        self.db.expire_supervisor_request(request_id)
        record = self.db.get(request['call_id'])
        if record['status'] in TERMINAL or request['call_id'] in self.stopping:
            return
        try:
            await self.send_call_update(request['call_id'], CallUpdateRequest(
                content='No verified supervisor reply arrived in time. Do not guess availability or confirm a booking. Offer to save a message for follow-up.',
                mode='instructions', update_id=uuid5(UUID(request_id), 'supervisor-timeout')))
        except Exception as exc:
            log_event('supervisor_timeout_update_failed', call_id=request['call_id'], error_type=type(exc).__name__)

    async def respond_to_supervisor(self, request_id: str, reply: SupervisorReply) -> dict:
        record = self.db.get(self.db.supervisor_request(request_id)['call_id'])
        if reply.outcome == 'appointment_confirmed' and not record['inbound_booking_allowed']:
            raise ValueError('This call has no standing booking authorization; ask the owner first')
        headings = {'information':'Supervisor verified information; no booking is confirmed:',
            'appointment_confirmed':'Supervisor verified the appointment was actually booked:',
            'needs_owner':'Supervisor needs owner approval; make no commitment:',
            'declined':'Supervisor declined; offer to take a message:'}
        update = CallUpdateRequest(content=headings[reply.outcome]+'\n'+reply.response,
                                   mode='say', update_id=UUID(request_id))
        request = self.db.answer_supervisor(request_id, reply.response, reply.outcome)
        try:
            result = await self.send_call_update(request['call_id'], update)
        except ValueError:
            with self.db.connect() as db:
                db.execute('UPDATE supervisor_requests SET delivery_error=? WHERE request_id=?', ('call_ending_or_ended',request_id))
            return {**self.db.supervisor_request(request_id), 'delivery_status':'not_delivered',
                    'note':'Answer retained for owner review. Do not repeat any booking. Caller delivery is unconfirmed.'}
        return {**request, 'delivery_status':result['status'], 'call_update_id':result['update_id'],
                'note':'Reply injection is not proof the caller heard it. Verify transcript; never repeat a booking on a delivery retry.'}

    async def stop_supervision(self):
        tasks = list(self.supervisor_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        if record.get('direction') == 'inbound':
            raise ValueError('Incoming message-taking calls cannot send keypad actions')
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
                'direction':record['direction'], 'message_id':record['message_id'],
                'inbound_mode':record['inbound_mode'], 'inbound_booking_allowed':bool(record['inbound_booking_allowed']),
                'voice_tools_enabled': self.settings.live_tools_enabled or record['direction'] == 'inbound',
                'call_updates_supported': True,
                'voice_control_mode': (('incoming_concierge_backend' if record['inbound_mode'] == 'concierge' else 'incoming_message_backend') if record['direction'] == 'inbound' else
                    ('live_responses_backend' if self.settings.live_tools_enabled else 'external_mcp_client')),
                'voice_control_note': (('Incoming concierge can request verified supervisor help or take a message; no direct calendar/keypad access.'
                    if record['inbound_mode']=='concierge' else 'Incoming callers can leave a message only; keypad and owner actions are unavailable.')
                    if record['direction'] == 'inbound' else
                    ('The live voice backend handles spoken keypad requests directly. '
                    'Do not wait for ChatGPT polling or send duplicate digits.' if self.settings.live_tools_enabled else
                    'The originating MCP client must send keypad actions.')),
                'live_url': self.settings.public_url(f'/live?call_id={call_id}')}

    def capture_inbound_fallback(self, call_id: str):
        """Keep interrupted incoming conversations visible without inventing a summary."""
        record = self.db.get(call_id)
        if record['direction'] != 'inbound':
            return
        for request in record['supervisor_requests']:
            if request['status'] == 'pending':
                self.db.expire_supervisor_request(request['request_id'])
        if record['message_id'] and self.db.message(record['message_id'])['capture_method'] == 'voice_tool':
            return
        text = ''.join(e['text'] for e in record['transcript'] if e['speaker'] == 'callee').strip()
        if text:
            self.db.save_message(call_id, text[-8000:], capture_method='transcript_fallback')

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
