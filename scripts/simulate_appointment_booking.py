"""Replay booking through two real Live audio peers without telephone calls.

Default: real calendar reads, private scratch journal, simulated calendar create.
--execute-approved-booking: one owner-authorized real booking, capped to the
exact private scenario and linked to its original completed call. The original
call is never reactivated and its expired request is never extended or replayed.
"""
import argparse
import asyncio
import base64
from contextlib import AsyncExitStack,suppress
from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time

from pydantic import BaseModel,ConfigDict,Field
from app.appointments.contracts import Appointment,CalendarEvent
from app.appointments.gateway import AppointmentGateway
from app.appointments.registry import payload,payload_hash,event_identity
from app.appointments.confirmation import exact_room,room_words
from app.appointments.evidence import speech_words
from app.appointments.schemas import SlotRequest
from app.calling.call_manager import CallManager
from app.calling.live_session import MediaBridge,live_client,start_session,optional_compaction_notice
from app.calling.prompts import session_config
from app.storage.database import Database
from scripts.simulate_appointment_lookup import (load_settings,readonly,seed_context,NoTelephone,
    LocalAudioTransport,EventTap,heard_details)


class BookingScenario(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_call_id:str
    appointment:SlotRequest
    script:list[str]=Field(min_length=1,max_length=10)
    timeout_seconds:int=Field(default=150,ge=30,le=180)


class CappedCalendar:
    """No arbitrary writes: one exact owner-approved payload, or a local dry run."""
    def __init__(self,provider,expected,execute=False):
        self.provider=provider;self.expected=expected;self.execute=execute
        self.creates=0;self.events={};self.dispatching=False;self.owner_authorize=lambda:None
    def __getattr__(self,name):return getattr(self.provider,name)
    def permits(self,value):
        actual,expected=payload(value),payload(self.expected)
        actual.pop('location');expected.pop('location')
        for data in (actual,expected):data['purpose']=tuple(w for w in speech_words(data['purpose']) if w not in {'a','an','the','to'})
        # "In person, room E-1066" and "room E 1066" are the same owner-approved
        # place. Never relax time/name/purpose or accept another room/address.
        def place(text):return text.removeprefix('In person, ').removeprefix('in person, ')
        return actual==expected and exact_room(room_words(place(value.location)),place(self.expected.location))
    async def create(self,identity,value,*,authorize):
        if self.creates or not self.permits(value):raise ValueError('Replay is capped to one exact approved appointment')
        authorize();self.creates+=1
        if self.execute:
            self.dispatching=True
            def dispatch_authorize():self.owner_authorize();authorize()
            try:return await self.provider.create(identity,value,authorize=dispatch_authorize)
            finally:self.dispatching=False
        self.events[identity.event]=CalendarEvent(identity,'dry-run-only',value.interval,
            location=value.location,participant_first=value.first_name,participant_last=value.last_name,
            disclosure='ordinary',ownership='phone-assistant',purpose=value.purpose,booking_details_verified=True)
    async def read_event(self,identity):
        return self.events[identity.event] if identity.event in self.events else await self.provider.read_event(identity)
    async def read_interval(self,interval,exclude=None):
        coverage=await self.provider.read_interval(interval,exclude=exclude)
        return replace(coverage,events=(*coverage.events,*(e for e in self.events.values() if e.identity!=exclude)))


def caller_config(settings,scenario,voice):
    v=scenario.appointment
    return {'model':settings.openai_live_model,'store':False,
        'audio':{'format':{'type':'audio/pcmu','rate':8000},'output':{'voice':voice}},'delegation':{'type':'client'},
        'instructions':'''You are the CALLER, never the office assistant. Listen to the greeting first.
You want to BOOK the appointment described in CALLER DATA. Use the first script line as your opening.
Supply the other details naturally when asked; do not read all data at once or invent a different time.
When offered the approved concrete date and time, say “That works for me, thank you.” once.
If offered any different date/time, decline. If the approved slot is busy, finish politely without booking; do not accept another slot.
Do not require any special booking phrase. Wait quietly while the office books it. If asked again whether that works,
say “Yes, please.” After hearing it is confirmed, say “Thank you, goodbye.” then remain silent.
Do not claim success yourself, impersonate the assistant, or ask for a second appointment. No tools.
CALLER DATA: '''+json.dumps({'appointment':v.model_dump(mode='json'),'script':scenario.script})}


def validate_execution(source,scenario,grant_path):
    if not grant_path:raise ValueError('Explicit private owner grant required for actual booking')
    p=Path(grant_path);info=p.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)&0o077:
        raise ValueError('Owner grant must be a private owner-controlled regular file')
    grant=json.loads(p.read_text());now=datetime.now(timezone.utc)
    if (grant.get('action')!='one_real_booking_without_telephone' or grant.get('source_call_id')!=scenario.source_call_id
            or grant.get('payload_hash')!=payload_hash(scenario.appointment.appointment())
            or not now<datetime.fromisoformat(grant['expires_at']) or grant.get('max_events')!=1):
        raise ValueError('Grant does not authorize this exact current booking')
    with readonly(source.database_path) as db:
        call=db.execute('SELECT status FROM calls WHERE call_id=?',(scenario.source_call_id,)).fetchone()
        if not call or call['status'] not in ('completed','failed'):raise ValueError('Replay needs a completed source call')
        if db.execute("SELECT 1 FROM calls WHERE status IN ('queued','initiated','ringing','in_progress')").fetchone():
            raise ValueError('Wait for zero active calls before the approved real replay')
    return hashlib.sha256(p.read_bytes()).hexdigest()


async def run_booking(source,scenario,output,*,execute=False,grant_path=None,caller_voice='marin'):
    output=Path(output).resolve()
    if output.exists() or output.is_relative_to(source.database_path.parent) or output.is_relative_to(Path(__file__).resolve().parents[1]):
        raise ValueError('Use a new private evidence directory outside source and production')
    grant_hash=validate_execution(source,scenario,grant_path) if execute else None
    output.mkdir(parents=True,mode=0o700);output.chmod(0o700)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'mode':'owner_approved_real_booking' if execute else 'dry_run',
        'source_call_id':scenario.source_call_id,'owner_grant_sha256':grant_hash,'transcript':[],'tools':[],'usage':{},
        'twilio_requests':0,'calendar_writes':0,'original_call_reactivated':False,'expired_request_extended':False}
    manager=None;tasks=[];bridge=None;calendar=None;caller_closed=asyncio.Event()
    try:
        with readonly(source.database_path) as db:
            row=db.execute('SELECT phone_number FROM calls WHERE call_id=?',(scenario.source_call_id,)).fetchone()
            if not row:raise ValueError('Source call not found')
            phone=row['phone_number']
        local=source.model_copy(update={'database_path':output/'calls.sqlite3','appointment_database_path':
            source.appointment_database_path if execute else output/'appointments.sqlite3',
            'appointments_enabled':False,'owner_calendar_coaching_enabled':False})
        manager=CallManager(local,Database(local.database_path),NoTelephone());seed_context(source.database_path,manager.db,phone)
        cid=manager.db.create_inbound('OWNER_AUTHORIZED_LOCAL_REPLAY',phone,3,10,'concierge',True)
        # An honest private replay view, never a fake/reopened production call.
        with manager.db.connect() as db:
            db.execute('BEGIN');db.execute('PRAGMA defer_foreign_keys=ON')
            db.execute('UPDATE calls SET call_id=? WHERE call_id=?',(scenario.source_call_id,cid))
            db.execute('UPDATE call_events SET call_id=? WHERE call_id=?',(scenario.source_call_id,cid))
        cid=scenario.source_call_id;manager.transition(cid,'in_progress');local.appointments_enabled=True
        manager.appointments=AppointmentGateway(manager);g=manager.appointments
        if g.store.prior_call_write(cid):raise ValueError('Existing operation: reconcile it; do not replay another create')
        calendar=CappedCalendar(g.service.provider,scenario.appointment.appointment(),execute);g.service.provider=calendar
        if execute:calendar.owner_authorize=lambda:validate_execution(source,scenario,grant_path)
        async def guard(request):
            if request.method=='GET' or (request.method=='POST' and request.url.host=='oauth2.googleapis.com' and request.url.path=='/token'):return
            if not (execute and calendar.dispatching and request.method=='POST' and request.url.path.endswith('/events') and report['calendar_writes']==0):
                raise ValueError('Calendar mutation outside the single approved dispatch')
            report['calendar_writes']+=1
        g.http.event_hooks['request'].append(guard)
        if execute:Database(source.database_path).emit(cid,'owner.authorized_booking_replay',
            {'grant_sha256':grant_hash,'evidence_directory':str(output),'payload_hash':payload_hash(scenario.appointment.appointment()),'stage':'started'})
        transport=LocalAudioTransport();config=session_config(local,manager.voice_record(cid))
        report['assistant_model']=config['model'];report['backend_model']=config['delegation']['responses']['model']
        async with live_client(source) as client,AsyncExitStack() as stack:
            assistant=await stack.enter_async_context(client.live.connect());caller=await stack.enter_async_context(client.live.connect())
            started=await start_session(assistant,config);peer=await start_session(caller,caller_config(source,scenario,caller_voice))
            if started['session']['id']==peer['session']['id']:raise ValueError('Expected independent Live sessions')
            manager.db.update(cid,openai_session_id=started['session']['id'])
            bridge=MediaBridge(transport,EventTap(assistant,'assistant',report),manager,cid,transport.stream,server_booking=True)
            manager.attach_bridge(cid,bridge);operation=bridge.tools.operation
            async def bounded(item):
                if item['name'] not in {'get_call_context','check_appointment_slot','find_first_available_slot','book_appointment','get_booking_status','end_call'}:
                    return {'state':'unsupported','instruction':'Only the one approved booking is in scope.'}
                before=time.monotonic();result=await operation(item)
                report['tools'].append({'name':item['name'],'arguments':json.loads(item['arguments']),
                    'result':result,'elapsed_seconds':round(time.monotonic()-before,3)})
                return result
            bridge.tools.operation=bounded
            async def receive_caller():
                async for event in EventTap(caller,'caller',report):
                    data=event.model_dump()
                    if event.type=='session.output_audio.delta':transport.to_assistant.append(base64.b64decode(data['delta'],validate=True))
                    elif event.type=='session.closed':caller_closed.set();return
                    elif event.type=='error' and not optional_compaction_notice(data):raise RuntimeError('Caller Live error')
                    elif event.type=='session.delegation.created':await caller.session.commentary.append(delegation_id=data['delegation']['id'],content='No tools. Continue as the caller; wait for the verified booking result.')
            conversation=asyncio.create_task(bridge.run(3));receiver=asyncio.create_task(receive_caller());pacer=asyncio.create_task(transport.pace(caller))
            async def done_booking():
                while not conversation.done():
                    await asyncio.sleep(.1)
                    op=g.store.prior_call_write(cid)
                    if not op or op['state']!='confirmed':continue
                    delivery=g.delivery_status(cid,op['operation_id'])
                    with g.store.connect() as db:audio=db.execute("SELECT * FROM appointment_voice_prompts WHERE call_id=? AND kind='result' ORDER BY rowid DESC LIMIT 1",(cid,)).fetchone()
                    heard=''.join(e['text'] for e in report['transcript'] if e['side']=='caller' and e['direction']=='input'
                        and audio and e.get('start_ms',0)>=audio['start_ms'])
                    tokens=speech_words(heard)
                    if delivery['spoken_observed'] and 'confirmed' in tokens and not {'not',"isn't",'unconfirmed'}&set(tokens) and not transport.to_caller.buffer:
                        report['caller_heard_confirmation']=True
                        transport.incoming.put_nowait({'event':'stop','streamSid':transport.stream});return await conversation
            tasks=[conversation,receiver,pacer,asyncio.create_task(done_booking())]
            try:
                async with asyncio.timeout(scenario.timeout_seconds):
                    done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for t in done:t.result()
                    if not conversation.done():raise RuntimeError('Audio peer stopped before booking finished')
            finally:
                conversation.cancel();pacer.cancel();await asyncio.gather(conversation,pacer,return_exceptions=True)
                with suppress(Exception):await caller.session.close();await asyncio.wait_for(caller_closed.wait(),5)
                receiver.cancel();await asyncio.gather(receiver,return_exceptions=True)
            report['audio_frames_each_direction']=transport.frames
            report['finalized']={'assistant':bridge.finalized.is_set(),'caller':caller_closed.is_set()}
        op=g.store.prior_call_write(cid);report['operation']=g.safe_operation(op) if op else None
        report['evaluation']={'passed':bool(op and op['state']=='confirmed' and calendar.creates==1 and report.get('caller_heard_confirmation') and all(report['finalized'].values())),
            'reason':'verified_single_booking' if op and op['state']=='confirmed' else 'booking_not_confirmed'}
        if execute and op:
            independent=await calendar.provider.read_event(event_identity(op))
            report['independent_provider_readback']=bool(independent and independent.state!='cancelled' and independent.booking_details_verified
                and independent.interval==scenario.appointment.appointment().interval and calendar.permits(
                    Appointment(independent.participant_first,independent.participant_last,
                        independent.purpose,independent.interval,independent.location)))
            report['evaluation']['passed'] &= report['independent_provider_readback']
    except Exception as exc:
        import traceback
        (output/'failure.txt').write_text(traceback.format_exc());(output/'failure.txt').chmod(0o600)
        report['evaluation']={'passed':False,'reason':type(exc).__name__}
    finally:
        for t in tasks:t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if manager:await manager.stop_supervision()
        if calendar:report['provider_creates']=calendar.creates
        (output/'report.json').write_text(json.dumps(report,indent=2));(output/'report.json').chmod(0o600)
        if execute:Database(source.database_path).emit(scenario.source_call_id,'owner.authorized_booking_replay',
            {'grant_sha256':grant_hash,'evidence_directory':str(output),'stage':'finished','evaluation':report['evaluation'],'calendar_writes':report['calendar_writes']})
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file',type=Path,required=True);parser.add_argument('--scenario',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--caller-voice',default='marin')
    parser.add_argument('--execute-approved-booking',action='store_true');parser.add_argument('--owner-grant',type=Path)
    args=parser.parse_args();os.umask(0o077)
    scenario=BookingScenario.model_validate_json(args.scenario.read_text())
    report=asyncio.run(run_booking(load_settings(args.env_file),scenario,args.output,execute=args.execute_approved_booking,
        grant_path=args.owner_grant,caller_voice=args.caller_voice))
    print(json.dumps({'evaluation':report['evaluation'],'calendar_writes':report['calendar_writes'],'twilio_requests':0,'report':str(args.output/'report.json')}))
    raise SystemExit(0 if report['evaluation']['passed'] else 1)


if __name__=='__main__':main()
