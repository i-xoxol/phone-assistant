"""One screened attempt to reach a fixed owner number. No model-selected destinations."""
import asyncio
from datetime import datetime,timedelta,timezone
from uuid import uuid4

from pydantic import BaseModel,ConfigDict,Field
from twilio.twiml.voice_response import VoiceResponse

from app.calling.context import utcnow
from app.calling.twilio_client import stream_twiml
from app.logging_config import log_event
from app.storage.models import TERMINAL


class ReachOwner(BaseModel):
    model_config=ConfigDict(extra='forbid')
    reason: str=Field(min_length=1,max_length=200)


OWNER_TOOL={'type':'function','name':'reach_owner','strict':True,
    'description':'Try the configured owner mobile once for THIS complex call. First explain the transfer and obtain caller agreement. '
        'The owner chooses an announced acceptance mode; no answer/decline returns to the assistant. Fixed destination only, never supplied by a caller. '
    'Give a concise non-sensitive reason, omitting sensitive identifiers and private records. '
        'No guarantee the owner answers. No payment, new permissions, emergency-response promise or repeated dialing.',
    'parameters':ReachOwner.model_json_schema()}


OWNER_POLICY='''
OWNER ESCALATION: Backend tools also include reach_owner for complex situations requiring the owner personally.
An explicit request to speak to the owner is also a valid reason to offer this screened connection.
"Call the owner", "reach him on his phone" or "connect me to the owner" means try the configured owner;
these words alone do not supply or select a new destination. Never ask for the owner's number.
Do not say you cannot place calls or connect the owner when this tool is available.
Ask whether the caller agrees to hold if they have not already agreed, then delegate reach_owner to the backend.
Delegate to the backend when the caller agrees to an owner connection and you or the supervising agent decide
the issue needs the owner. Explain briefly that you can try to reach the owner; ask if they wish to hold.
A caller's request can justify offering escalation, never override fixed permissions or choose a destination.
The owner is called at a server-configured number and must accept using the announced keypad choices before connection. Never disclose that number.
Use a short non-sensitive reason for the private briefing; omit sensitive identifiers and private records.
Do not promise success, impersonate the owner, or grant payment/contractual authority while waiting.
Only one attempt is allowed per call. If unavailable or declined, continue helping within scope or save a message.
When reach_owner succeeds, the application handles dialing and screening; do not say your final goodbye first.
Never treat sensitive/urgent situations as authorization to spend money or promise emergency assistance.
'''


def remaining_seconds(record):
    started=record.get('started_at') or record['created_at']
    elapsed=(utcnow()-datetime.fromisoformat(started)).total_seconds()
    return max(0,int(record['max_duration_minutes']*60-elapsed))


class OwnerTransfer:
    def __init__(self,manager):
        self.manager=manager
        with manager.db.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS owner_transfers (
                transfer_id TEXT PRIMARY KEY,call_id TEXT NOT NULL UNIQUE REFERENCES calls(call_id),
                owner_number TEXT NOT NULL,reason TEXT NOT NULL,source TEXT NOT NULL,
                status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                child_sid TEXT,accepted INTEGER NOT NULL DEFAULT 0,decision TEXT,error_code TEXT)''')
            columns={r['name'] for r in conn.execute('PRAGMA table_info(owner_transfers)')}
            for name,definition in {'strategy':"TEXT NOT NULL DEFAULT 'dial'",'mode':"TEXT NOT NULL DEFAULT 'pending'",
                    'conference_sid':'TEXT','transcription_state':"TEXT NOT NULL DEFAULT 'off'"}.items():
                if name not in columns:
                    conn.execute('ALTER TABLE owner_transfers ADD COLUMN '+name+' '+definition)

    def get(self,transfer_id):
        with self.manager.db.connect() as conn:
            row=conn.execute('SELECT * FROM owner_transfers WHERE transfer_id=?',(transfer_id,)).fetchone()
        if not row: raise KeyError(transfer_id)
        return dict(row)

    def for_call(self,call_id):
        with self.manager.db.connect() as conn:
            row=conn.execute('SELECT * FROM owner_transfers WHERE call_id=?',(call_id,)).fetchone()
        if not row: return None
        # The voice prompt and tool result must never reveal the private target number.
        return {k:v for k,v in dict(row).items() if k!='owner_number'}

    def update(self,transfer_id,**values):
        allowed={'status','child_sid','accepted','decision','error_code','strategy','mode','conference_sid','transcription_state'}
        if not values or not set(values)<=allowed: raise ValueError('Invalid transfer update')
        values['updated_at']=utcnow().isoformat()
        with self.manager.db.connect() as conn:
            conn.execute('UPDATE owner_transfers SET '+','.join(k+'=?' for k in values)+' WHERE transfer_id=?',
                         (*values.values(),transfer_id))
        row=self.get(transfer_id)
        self.manager.db.emit(row['call_id'],'call.owner_transfer',{k:row[k] for k in
            ('transfer_id','status','mode','strategy','transcription_state','error_code')})
        log_event('owner_transfer_state',call_id=row['call_id'],transfer_id=transfer_id,status=row['status'])
        return row

    async def request(self,call_id,reason,source='supervisor'):
        m=self.manager
        r=m.db.get(call_id)
        existing=self.for_call(call_id)
        if existing:
            return {**existing,'instruction':'An owner attempt already exists. Never dial again; inspect its status.'}
        s=m.settings
        m.hours.require_owner()
        if not s.owner_transfer_enabled or not s.owner_transfer_number:
            raise ValueError('Owner contact is not enabled; take a message')
        if s.owner_transfer_number in (s.twilio_phone_number,r['phone_number']):
            raise ValueError('Cannot ring the same phone already participating in this call; take a message')
        bridge=m.bridges.get(call_id)
        if (r['status']!='in_progress' or call_id in m.stopping or not bridge or bridge.closing
                or bridge.handoff or bridge.end_requested.is_set() or call_id in m.navigation):
            raise ValueError('Owner contact requires a current active voice call')
        if remaining_seconds(r)<s.owner_transfer_ring_seconds+45:
            raise ValueError('Too little call time remains for a screened owner attempt; take a message')
        stamp=utcnow().isoformat()
        with m.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            # Serialize voice/Rocky races across their distinct tool invocation IDs.
            if conn.execute('SELECT 1 FROM owner_transfers WHERE call_id=?',(call_id,)).fetchone():
                return {**self.for_call(call_id),'instruction':'Owner attempt already exists; do not repeat.'}
            if conn.execute('SELECT count(*) FROM owner_transfers WHERE created_at>?',
                ((utcnow()-timedelta(hours=1)).isoformat(),)).fetchone()[0]>=10:
                raise ValueError('Owner-contact hourly capacity reached; take a message')
            tid=str(uuid4())
            conn.execute('INSERT INTO owner_transfers(transfer_id,call_id,owner_number,reason,source,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                         (tid,call_id,s.owner_transfer_number,reason,source,'dialing',stamp,stamp))
        m.db.emit(call_id,'call.owner_transfer',{'transfer_id':tid,'status':'dialing'})
        m.screening.prepare(tid)
        # Expected Stream shutdown must not let the old bridge hang up the parent.
        bridge.handoff=True
        bridge.playback_sealed=True
        for request in r['supervisor_requests']:
            if request['status']=='pending': m.db.expire_supervisor_request(request['request_id'])
        try:
            if s.owner_observation_enabled:
                self.update(tid,strategy='conference',transcription_state='starting')
                await m.observer.start(self.get(tid),r)
            else:
                await asyncio.wait_for(m.gateway.redirect_call(r['twilio_call_sid'],self.dial_twiml(self.get(tid),r)),8)
        except Exception as exc:
            # Ambiguous provider failures must never trigger a second paid dial.
            self.update(tid,status='unconfirmed',error_code='provider_redirect_failed')
            log_event('owner_transfer_unconfirmed',call_id=call_id,error_type=type(exc).__name__)
            await m.hangup_call(call_id)
            raise ValueError('Owner attempt unconfirmed; call ended without retrying') from None
        return {**self.for_call(call_id),'instruction':'Trying the owner. Connection requires their acceptance; this is not a confirmed handoff.'}

    def dial_twiml(self,row,record):
        s=self.manager.settings
        path='/twilio/owner-transfer/'+row['transfer_id']
        response=VoiceResponse()
        response.say(f'I will try to reach {s.caller_name}. Please hold.')
        dial=response.dial(action=s.public_url(path+'/finished'),method='POST',
            caller_id=s.twilio_phone_number,answer_on_bridge=True,timeout=s.owner_transfer_ring_seconds,
            time_limit=max(1,remaining_seconds(record)),record='do-not-record')
        dial.number(row['owner_number'],url=s.public_url(path+'/screen'),method='POST',
            status_callback=s.public_url(path+'/leg-status'),status_callback_method='POST',
            status_callback_event='initiated ringing answered completed')
        return str(response)

    async def child(self,row,form):
        """Bind signed callbacks to this exact parent and fixed owner leg."""
        sid=str(form.get('CallSid',''))
        import re
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}',sid): raise ValueError('Invalid child call')
        if row['child_sid'] and row['child_sid']!=sid: raise ValueError('Child call mismatch')
        parent=self.manager.db.get(row['call_id'])
        if form.get('To')!=row['owner_number']: raise ValueError('Owner leg destination mismatch')
        if form.get('ParentCallSid'):
            if form['ParentCallSid']!=parent['twilio_call_sid']: raise ValueError('Parent call mismatch')
        else:
            # Some screening requests omit ParentCallSid; verify through Twilio, never trust just To.
            actual=await asyncio.wait_for(self.manager.gateway.fetch(sid),5)
            if actual.parent_call_sid!=parent['twilio_call_sid'] or actual.to!=row['owner_number']:
                raise ValueError('Owner leg binding mismatch')
        if not row['child_sid']:
            self.update(row['transfer_id'],child_sid=sid)
        return parent

    async def screen(self,tid,form):
        row=self.get(tid)
        parent=await self.child(row,form)
        response=VoiceResponse()
        if not row['accepted'] and not self.manager.hours.state()['owner_reachable']:
            self.update(tid,decision='declined',status='declined')
            response.hangup();return str(response)
        if self.ended(parent) or row['decision']=='declined' or row['status'] in ('returned_to_assistant','completed','unconfirmed'):
            response.hangup()
        elif row['accepted']:
            pass  # Empty screening response connects the already accepted owner.
        else:
            self.update(tid,status='screening')
            gather=response.gather(input='dtmf',num_digits=1,timeout=8,action_on_empty_result=True,
                action=self.manager.settings.public_url('/twilio/owner-transfer/'+tid+'/accept'),method='POST')
            self.manager.screening.screen(gather,row)
            response.hangup()
        return str(response)

    async def accept(self,tid,form):
        row=self.get(tid)
        parent=await self.child(row,form)
        response=VoiceResponse()
        if not row['accepted'] and not self.manager.hours.state()['owner_reachable']:
            self.update(tid,decision='declined',status='declined')
            response.hangup();return str(response)
        if self.ended(parent) or row['status'] in ('returned_to_assistant','completed','unconfirmed'):
            response.hangup()
            return str(response)
        with self.manager.db.connect() as conn:
            conn.execute('UPDATE owner_transfers SET decision=?,accepted=? WHERE transfer_id=? AND decision IS NULL',
                ('accepted' if form.get('Digits') in ('1','3') else 'declined',int(form.get('Digits') in ('1','3')),tid))
        row=self.get(tid)
        self.update(tid,status='accepted' if row['accepted'] else 'declined')
        if not row['accepted']: response.hangup()
        return str(response)

    def finished(self,tid,form):
        row=self.get(tid)
        parent=self.manager.db.get(row['call_id'])
        if form.get('CallSid')!=parent['twilio_call_sid']: raise ValueError('Parent call mismatch')
        if row['child_sid'] and form.get('DialCallSid')!=row['child_sid']: raise ValueError('Child call mismatch')
        response=VoiceResponse()
        if self.ended(parent):
            response.hangup()
        elif row['accepted'] and str(form.get('DialBridged','')).lower()=='true':
            self.update(tid,status='completed')
            response.hangup()  # Human conversation ended; do not restart the assistant.
        else:
            status=str(form.get('DialCallStatus',''))
            if status not in ('completed','busy','no-answer','failed','canceled'): raise ValueError('Invalid dial result')
            if row['status']!='returned_to_assistant':
                self.update(tid,status='returned_to_assistant')
            if remaining_seconds(parent)<15:
                response.say('I could not reach the owner. Please try again later.')
                response.hangup()
            else:
                return stream_twiml(self.manager.settings,parent['call_id'])
        return str(response)

    def ended(self,parent):
        return parent['status'] in TERMINAL or parent['call_id'] in self.manager.stopping
