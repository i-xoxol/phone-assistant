"""OpenAI Realtime answering sessions over the telephone media bridge."""
import asyncio
import json
import re
from contextlib import asynccontextmanager

from websockets.asyncio.client import connect

from app.calling.elevenlabs_session import RealtimeConnection, realtime_config
from app.calling.live_session import BridgeFailure
from app.logging_config import log_event


class OpenAIRealtimeConnection(RealtimeConnection):
    provider = 'openai_realtime'


def openai_realtime_config(settings, record, selection):
    config = realtime_config(settings, record, selection)
    config['max_output_tokens'] = config.pop('max_response_output_tokens')
    config.pop('max_duration_seconds')
    config['reasoning'] = {'effort': 'low'}
    config['audio']['input']['transcription'] = {'model': 'gpt-4o-mini-transcribe'}
    config['tools'] = [{key: tool[key] for key in ('type', 'name', 'description', 'parameters') if key in tool}
                       for tool in config['tools']]
    return config


@asynccontextmanager
async def started_openai_realtime_connection(settings, manager, record, selection):
    if record.get('direction') == 'inbound' and not record.get('browser_test'):
        try:
            await asyncio.wait_for(manager.gateway.prepare_inbound(
                record['twilio_call_sid'], record['max_duration_minutes']), 5)
        except Exception as exc:
            raise BridgeFailure('incoming_duration_limit_failed') from exc

    record = manager.voice_record(record['call_id'])
    config = openai_realtime_config(settings, record, selection)
    async with connect(f"wss://api.openai.com/v1/realtime?model={selection['model']}",
                       additional_headers={'Authorization': 'Bearer ' + settings.openai_api_key.get_secret_value()},
                       open_timeout=10, close_timeout=2, max_size=2**20) as socket:
        connection = OpenAIRealtimeConnection(socket, config, record,
            current_record=lambda: manager.db.get(record['call_id']),
            claim_opening=lambda: manager.voice_backends.claim_opening(record['call_id']))
        async with asyncio.timeout(10):
            await connection.send({'type': 'session.update', 'session': config})
            while True:
                event = json.loads(await socket.recv())
                if event['type'] == 'error':
                    error = event.get('error', {})
                    code = str(error.get('code', 'unknown'))
                    parameter = str(error.get('param', 'unknown'))
                    log_event('openai_realtime_initialization_failed', call_id=record['call_id'],
                              code=code if re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'unknown',
                              parameter=parameter if re.fullmatch(r'session[A-Za-z0-9_.\[\]-]{0,120}', parameter) else 'unknown')
                    raise BridgeFailure('openai_realtime_initialization_rejected')
                if event['type'] == 'session.updated':
                    audio = event['session']['audio']
                    if any(audio.get(channel, {}).get('format', {}).get('type') != 'audio/pcmu'
                           for channel in ('input', 'output')):
                        raise BridgeFailure('openai_realtime_codec_mismatch')
                    break
        log_event('openai_realtime_connected', call_id=record['call_id'], model=selection['model'])
        try:
            yield connection, record, {'session': {'id': event['session']['id']}}
        finally:
            await connection.close()
