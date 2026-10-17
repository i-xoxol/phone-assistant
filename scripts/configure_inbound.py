"""Inspect or set this instance's owned Twilio number routing. Dry-run by default."""
import argparse
import json
import os
from datetime import datetime, timezone

from twilio.rest import Client

from app.config import ROOT, Settings


def configure(settings, client, *, apply=False, replace_existing=False):
    if settings.missing():
        raise ValueError('Complete the required .env settings first')
    if apply and not settings.inbound_enabled:
        raise ValueError('Enable and test INBOUND_ENABLED before changing number routing')
    numbers = client.incoming_phone_numbers.list(phone_number=settings.twilio_phone_number, limit=2)
    if len(numbers) != 1 or numbers[0].phone_number != settings.twilio_phone_number:
        raise ValueError('Exactly one owned, matching Twilio phone number is required')
    number = numbers[0]
    desired = {'voice_url':settings.public_url('/twilio/incoming'), 'voice_method':'POST',
               'status_callback':settings.public_url('/twilio/incoming/status'), 'status_callback_method':'POST'}
    fields = (*desired, 'voice_application_sid', 'voice_fallback_url', 'voice_fallback_method')
    previous = {key:(getattr(number,key,None) or '') for key in fields}
    defaults = ('', 'https://demo.twilio.com/welcome/voice/')
    custom = bool(previous['voice_application_sid'] or
                  previous['voice_url'] not in (*defaults, desired['voice_url']) or
                  previous['status_callback'] not in ('', desired['status_callback']) or
                  previous['voice_fallback_url'])
    if custom and not replace_existing:
        raise ValueError('Custom voice routing exists; review it before using --replace-existing')
    if previous['voice_application_sid'] or previous['voice_fallback_url']:
        raise ValueError('A TwiML Application or fallback is configured; reconcile it manually first')
    if not apply:
        return {'applied':False,'current':previous,'desired':desired,'sms_unchanged':True}
    folder=ROOT/'data'/'routing-backups'
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=folder/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')+'.json')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as output:
        json.dump({'number_sid':number.sid,'previous':previous,'desired':desired},output,indent=2)
    number.update(**desired)
    confirmed=client.incoming_phone_numbers(number.sid).fetch()
    if any(getattr(confirmed,key,None)!=value for key,value in desired.items()):
        raise ValueError('Twilio routing verification failed; inspect the saved backup before retrying')
    return {'applied':True,'backup':str(path),'voice_url':desired['voice_url'],'sms_unchanged':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--replace-existing',action='store_true',help='Allow replacing reviewed custom voice/status URLs')
    args=parser.parse_args()
    settings=Settings()
    client=Client(settings.twilio_account_sid,settings.twilio_auth_token.get_secret_value())
    try:
        print(json.dumps(configure(settings,client,apply=args.apply,replace_existing=args.replace_existing),indent=2))
    except Exception as exc:
        # Provider exception strings can contain account identifiers; keep them private.
        message=str(exc) if type(exc) is ValueError else type(exc).__name__
        raise SystemExit('Incoming routing configuration failed: '+message) from None


if __name__=='__main__':
    main()
