"""Explicit manual integration test: this script places ONE real outbound call."""
import argparse
import json
import sys
import time

import httpx

from app.config import ROOT, Settings
from app.storage.models import CallRequest, TERMINAL


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--to', required=True, help='Your consenting test recipient in E.164 format')
    parser.add_argument('--recipient', default='Consenting test recipient')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    args = parser.parse_args()
    settings = Settings()
    if missing := settings.missing():
        print('Missing configuration: ' + ', '.join(missing))
        return 1
    request = CallRequest(
        phone_number=args.to, recipient=args.recipient, max_duration_minutes=2,
        objective='Conduct a brief audio test. Ask the person to interrupt you while you count to ten, then ask how the audio sounded.',
        context='This is a development test, not a business call. Have a friendly conversation with the recipient and answer their questions. If the recipient is the caller, address them directly as their assistant.',
        constraints=['Do not spend money or agree to anything.',
                     'Only disclose the caller name. After the test, thank the person and initiate ending the call yourself with the closing phrase from your system instructions.'])
    call_id = None
    with httpx.Client(base_url=args.base_url, timeout=25,
                      headers={'Authorization': 'Bearer ' + settings.local_api_token.get_secret_value()}) as client:
        try:
            # Creation is never automatically retried. Ambiguous errors need Twilio inspection.
            response = client.post('/calls', json=request.model_dump())
            response.raise_for_status()
            result = response.json()
            call_id = result['call_id']
            print('Call ID: ' + call_id)
            deadline = time.monotonic() + 180
            previous = None
            while time.monotonic() < deadline:
                response = client.get(f'/calls/{call_id}', params={'refresh': 'true'})
                response.raise_for_status()
                status = response.json()['status']
                if status != previous:
                    print('Status: ' + status)
                    previous = status
                if status in TERMINAL:
                    # Twilio's completion callback may precede Live finalization.
                    if status == 'completed':
                        time.sleep(6)
                    response = client.get(f'/calls/{call_id}/result')
                    response.raise_for_status()
                    record = response.json()
                    path = ROOT / 'data' / f'manual-test-{call_id}.json'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(record, indent=2), encoding='utf-8')
                    print('Record saved locally: ' + str(path))
                    print('Verify greeting, two-way audio, interruptions, constraints, transcript and finalization against README.')
                    return 0 if (status == 'completed' and record['openai_session_id']
                                 and record['voice_finalized'] and not record['error_message']) else 1
                time.sleep(2)
            print('Timed out waiting for completion; requesting hangup.')
        except KeyboardInterrupt:
            print('Interrupted; requesting hangup.')
        except httpx.HTTPError as exc:
            print(f'HTTP request failed ({type(exc).__name__}); inspect Twilio before retrying creation.')
        finally:
            if call_id:
                try:
                    client.post(f'/calls/{call_id}/hangup').raise_for_status()
                except httpx.HTTPError:
                    print('Hangup unconfirmed; check the Twilio Console.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
