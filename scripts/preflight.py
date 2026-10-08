"""Configuration check; --network checks providers and tunnel without dialing."""
import argparse
import asyncio
import base64
import re
import sys

import httpx
import openai

from app.calling.live_session import live_client, start_session
from app.calling.prompts import session_config
from app.calling.twilio_client import TwilioGateway
from app.config import Settings
from app.storage.models import E164


async def check_network(settings):
    gateway = TwilioGateway(settings)
    client = gateway.client()
    numbers = await asyncio.to_thread(client.incoming_phone_numbers.list,
                                     phone_number=settings.twilio_phone_number, limit=1)
    if not numbers or not numbers[0].capabilities.get('voice'):
        raise ValueError('TWILIO_PHONE_NUMBER must be a voice-capable number on this account')
    print('Twilio authentication and owned voice number: OK')
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as http:
        response = await http.get(settings.public_url('/health'))
        response.raise_for_status()
        if not response.json().get('ready'):
            raise ValueError('Public tunnel points to an unconfigured service')
    print('Public HTTPS health endpoint: OK (media WebSocket still needs a real call)')
    record = {'recipient': 'Preflight', 'objective': 'Connectivity check only',
              'context': '', 'constraints': ['No external actions']}
    async with live_client(settings) as client:
        async with client.live.connect() as connection:
            await start_session(connection, session_config(settings, record))
            print('GPT-Live session.start/session.started: OK')
            # Pace 0.5 seconds of silence, never a burst or a real telephone call.
            for _ in range(25):
                await connection.session.input_audio.append(audio=SILENCE)
                await asyncio.sleep(0.02)
            await connection.session.close()
            async with asyncio.timeout(10):
                async for event in connection:
                    if event.type == 'session.closed':
                        print('GPT-Live graceful finalization: OK')
                        return
                    if event.type == 'error':
                        raise RuntimeError('GPT-Live returned a session error')
            raise RuntimeError('No session.closed received')


SILENCE = base64.b64encode(bytes([255]) * 160).decode('ascii')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network', action='store_true',
                        help='Contact Twilio, public tunnel and GPT-Live; may incur a small OpenAI charge; never dial')
    args = parser.parse_args()
    settings = Settings()
    print(f'OpenAI SDK {openai.__version__}; native Live support: '
          f'{hasattr(openai.AsyncOpenAI, "live")}')
    if missing := settings.missing():
        print('Missing configuration: ' + ', '.join(missing))
        return 1
    if not re.fullmatch(E164, settings.twilio_phone_number):
        print('TWILIO_PHONE_NUMBER must be in E.164 format')
        return 1
    if settings.callback_number and not re.fullmatch(E164, settings.callback_number):
        print('CALLBACK_NUMBER must be empty or in E.164 format')
        return 1
    print('Required settings: configured (values hidden)')
    if args.network:
        try:
            asyncio.run(check_network(settings))
        except Exception as exc:
            print(f'Network preflight failed ({type(exc).__name__}). Check provider access, credentials and tunnel; values hidden.')
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
