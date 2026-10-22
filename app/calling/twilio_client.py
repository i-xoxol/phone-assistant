import asyncio
from urllib.parse import urlencode

from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse

from app.config import Settings


def stream_twiml(settings: Settings, call_id: str, digits: str = '') -> str:
    response = VoiceResponse()
    if digits:
        response.play(digits=digits)
    response.connect().stream(url=settings.public_url(f'/twilio/media/{call_id}', websocket=True),
        status_callback=settings.public_url(f'/twilio/stream-status/{call_id}'), status_callback_method='POST')
    response.hangup()
    return str(response)


class TwilioGateway:
    def __init__(self, settings: Settings):
        self.settings = settings

    def client(self) -> Client:
        # No automatic retry: ambiguous create failures may already have placed a call.
        return Client(self.settings.twilio_account_sid,
                      self.settings.twilio_auth_token.get_secret_value(),
                      http_client=TwilioHttpClient(timeout=15, max_retries=0))

    async def place(self, call_id: str, phone_number: str, minutes: int):
        return await asyncio.to_thread(
            self.client().calls.create, to=phone_number, from_=self.settings.twilio_phone_number,
            url=self.settings.public_url(f'/twilio/voice/{call_id}'), method='POST',
            status_callback=self.settings.public_url(f'/twilio/status/{call_id}'),
            status_callback_event=['initiated', 'ringing', 'answered', 'completed'],
            status_callback_method='POST', timeout=30, time_limit=minutes * 60, record=False)

    async def fetch(self, sid: str):
        return await asyncio.to_thread(self.client().calls(sid).fetch)

    async def redirect_call(self,sid,twiml):
        return await asyncio.to_thread(self.client().calls(sid).update,twiml=twiml)

    async def ring_owner(self, row, seconds):
        path='/twilio/owner-transfer/'+row['transfer_id']
        return await asyncio.to_thread(self.client().calls.create,
            to=row['owner_number'],from_=self.settings.twilio_phone_number,
            url=self.settings.public_url(path+'/screen'),method='POST',
            status_callback=self.settings.public_url(path+'/leg-status'),status_callback_method='POST',
            status_callback_event=['initiated','ringing','answered','completed'],
            timeout=self.settings.owner_transfer_ring_seconds,time_limit=seconds,record=False)

    async def whisper(self, conference_sid, owner_sid, url):
        # A participant announcement targets only this participant, never the room.
        return await asyncio.to_thread(self.client().conferences(conference_sid).participants(owner_sid).update,
            announce_url=url,announce_method='POST')

    async def add_coach(self,row,record,seconds):
        """A TwiML application participant audible only to the accepted owner's leg."""
        params=urlencode({'coach_id':record['coach_id'],'nonce':record['nonce']})
        return await asyncio.to_thread(self.client().conferences(row['conference_sid']).participants.create,
            from_=self.settings.twilio_phone_number,
            to='app:'+self.settings.owner_coaching_application_sid+'?'+params,
            label='rt-'+record['coach_id'],coaching=True,call_sid_to_coach=row['child_sid'],
            start_conference_on_enter=False,end_conference_on_exit=False,beep='false',
            early_media=True,record=False,max_participants=3,jitter_buffer_size='small',time_limit=seconds)

    async def coach_participant(self,conference_sid,label_or_sid):
        return await asyncio.to_thread(self.client().conferences(conference_sid).participants(label_or_sid).fetch)

    async def prepare_inbound(self, sid: str, minutes: int):
        # Only call AFTER the signed media start proves Twilio answered.
        # Number-level configuration already supplies the status callback.
        # Changing a per-call callback requires Url and would redirect the call.
        return await asyncio.to_thread(self.client().calls(sid).update,
            time_limit=minutes*60)

    async def stop_recording(self,sid):
        return await asyncio.to_thread(self.client().calls(sid).recordings('Twilio.CURRENT').update,status='stopped')

    async def recording_audio(self,rid):
        from app.calling.recordings import fetch_recording_audio
        return await fetch_recording_audio(self.settings,rid)

    async def press_digits(self, sid: str, call_id: str, digits: str):
        return await asyncio.to_thread(self.client().calls(sid).update,
                                      twiml=stream_twiml(self.settings, call_id, digits))

    async def hangup(self, sid: str):
        # Twilio cancels pre-answer calls and completes connected calls.
        call = await self.fetch(sid)
        if call.status in ('completed', 'failed', 'busy', 'no-answer', 'canceled'):
            return call
        target = 'canceled' if call.status in ('queued', 'initiated', 'ringing') else 'completed'
        return await asyncio.to_thread(self.client().calls(sid).update, status=target)

    async def hangup_fast(self, sid: str, known_status: str):
        target = 'completed' if known_status == 'in_progress' else 'canceled'
        try:
            return await asyncio.to_thread(self.client().calls(sid).update, status=target)
        except Exception:
            # Pre-answer state may have raced an answer or provider completion.
            return await self.hangup(sid)
