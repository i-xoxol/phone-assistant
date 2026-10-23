"""Server-owned voice booking state, derived from durable proposals and operations.

The voice model collects fields and speaks one proposal. It never chooses the
operation identity. Caption notifications resume only a live, bound proposal;
the existing booking service remains the sole calendar write boundary.
"""
import asyncio,time
from uuid import UUID,uuid5
from app.appointments.service import ConsentRequired
from app.appointments.evidence import ambiguous_agreement
from app.storage.models import CallUpdateRequest
from app.logging_config import log_event


class VoiceBookingWorkflow:
    settle_seconds=.6

    def __init__(self,gateway):
        self.g=gateway;self.tasks={};self.generations={}
        with self.g.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS appointment_voice_workflows(
                call_id TEXT PRIMARY KEY,draft_id TEXT NOT NULL,draft_revision INTEGER NOT NULL,
                check_id TEXT NOT NULL,updated_at TEXT NOT NULL)''')

    def bind(self,call_id,result):
        if not result.get('draft_id') or not result.get('check_id'):return
        draft=self.g.service.bound(call_id,result['draft_id'])
        with self.g.store.connect() as db:
            db.execute('INSERT INTO appointment_voice_workflows VALUES(?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET '
                'draft_id=excluded.draft_id,draft_revision=excluded.draft_revision,check_id=excluded.check_id,updated_at=excluded.updated_at',
                (call_id,draft['draft_id'],draft['revision'],result['check_id'],self.g.clock().isoformat()))

    def bound(self,call_id):
        with self.g.store.connect() as db:
            row=db.execute('SELECT * FROM appointment_voice_workflows WHERE call_id=?',(call_id,)).fetchone()
        if not row:return None
        draft=self.g.service.bound(call_id,row['draft_id'])
        if draft['revision']!=row['draft_revision']:return None
        check=self.g.store.get('appointment_checks','check_id',row['check_id'])
        if check['draft_id']!=draft['draft_id'] or check['draft_revision']!=draft['revision']:return None
        return draft,check

    def waiting(self,phase,reason):
        return {'state':'needs_consent' if phase in ('awaiting_readback','awaiting_answer','awaiting_captions') else 'needs_details',
            'phase':phase,'reason':reason,'calendar_write_attempted':False,'readback_queued':False,
            'server_managed':True,'instruction':'Nothing is confirmed yet. The server owns the current proposal and booking IDs. '
                'Do not repeat the proposal or invent an operation ID. Wait for the complete caller answer; '
                'clarify only a missing or changed detail. Natural agreement is sufficient.'}

    def pending(self,call_id):
        bound=self.bound(call_id)
        if not bound:return self.waiting('collecting_details','no_current_voice_proposal')
        draft,check=bound
        try:self.g.service.current(draft)
        except ValueError:return self.waiting('inactive','call_or_request_no_longer_authorized')
        if check['decision']!='available':
            return {**self.waiting('checking',check['reason']),'state':'conflict' if check['decision']=='busy' else 'needs_owner'}
        evidence=self.g.consent(draft,None)
        packet=self.g.voice_confirmation.proposal(draft)
        if packet and packet['state'] in ('failed','withdrawn'):
            return {**self.waiting('collecting_details','proposal_withdrawn_or_audio_unconfirmed'),
                'instruction':'Nothing is booked. Respond to the caller’s correction or explain the failed audio delivery once. Collect only missing or changed details; do not claim you are waiting for a booking confirmation.'}
        if evidence.readback_observed and evidence.affirmative:
            return {'state':'ready','phase':'consented','server_managed':True,'calendar_write_attempted':False}
        return self.waiting('awaiting_answer' if evidence.readback_observed else 'awaiting_readback',
            'complete_proposal_and_current_caller_agreement_required')

    async def status(self,call_id):
        operation=self.g.store.prior_call_write(call_id)
        if operation:
            current=self.bound(call_id)
            if current and (operation['draft_id']!=current[0]['draft_id'] or operation['payload_hash']!=current[0]['payload_hash']):
                return {'state':'needs_owner','phase':'existing_operation','operation_id':operation['operation_id'],
                    'reason':'existing_call_write_requires_reconciliation','server_managed':True,
                    'instruction':'This call already has another booking operation. Do not confirm this new proposal or create another event; reconcile the existing operation with the owner.'}
            result=await self.g.status(call_id,operation['operation_id'])
            result['server_managed']=True;return result
        return self.pending(call_id)

    async def book(self,call_id,args=None):
        bound=self.bound(call_id)
        if not bound:return self.waiting('collecting_details','no_current_voice_proposal')
        draft,check=bound
        # Older clients may still send the former arguments. Never substitute a
        # different call/draft/check or accept a model-selected write identity.
        if args and (str(args.get('draft_id'))!=draft['draft_id'] or str(args.get('check_id'))!=check['check_id']):
            return self.waiting('awaiting_answer','use_server_owned_proposal_without_arguments')
        operation=self.g.store.prior_call_write(call_id)
        if operation:return await self.status(call_id)
        pending=self.pending(call_id)
        if pending['state']!='ready':return pending
        try:
            result=await self.g.book(call_id,dict(draft_id=draft['draft_id'],check_id=check['check_id'],
                idempotency_key='voice-'+draft['draft_id']))
        except ConsentRequired:return self.pending(call_id)
        result['server_managed']=True;return result

    def notify(self,bridge,*,caller=True):
        cid=bridge.call_id
        if not self.bound(cid):return
        self.generations[cid]=self.generations.get(cid,0)+(1 if caller else 0)
        current=self.tasks.get(cid)
        if current and not current.done():return
        task=asyncio.create_task(self.advance(bridge));self.tasks[cid]=task
        self.g.m.tool_tasks.add(task)
        def done(t):
            self.g.m.tool_tasks.discard(t)
            if self.tasks.get(cid) is t:self.tasks.pop(cid,None)
        task.add_done_callback(done)

    async def clarify(self,call_id):
        bound=self.bound(call_id)
        if not bound:return
        draft,check=bound
        if self.g.voice_confirmation.proposal(draft):return
        if not self.g.consent(draft,None).readback_observed:return
        with self.g.m.db.connect() as db:
            rows=[dict(r) for r in db.execute("SELECT id,speaker,text FROM transcript_events WHERE call_id=? ORDER BY start_ms,id",(call_id,))]
        # Use only the latest contiguous caller answer, not the earlier intake.
        caller_blocks=[]
        for row in rows:
            if caller_blocks and caller_blocks[-1]['speaker']==row['speaker']:caller_blocks[-1]['text']+=row['text'];caller_blocks[-1]['id']=row['id']
            else:caller_blocks.append(dict(row))
        caller=next((r for r in reversed(caller_blocks) if r['speaker']=='callee'),None)
        if not caller or not ambiguous_agreement(caller['text']):return
        uid=uuid5(UUID(draft['draft_id']),'clarify-agreement-'+str(draft['revision'])+'-'+str(caller['id']))
        if self.g.m.db.call_update(call_id,str(uid)):return
        await self.g.m.send_call_update(call_id,CallUpdateRequest(mode='instructions',update_id=uid,
            content='The exact appointment proposal was already spoken. Do not repeat its details. Ask only: Does that work for you? Then wait. Nothing is booked yet; a refusal or correction must be respected.'))

    async def advance(self,bridge):
        cid=bridge.call_id
        try:
            while True:
                generation=self.generations.get(cid,0)
                await asyncio.sleep(self.settle_seconds)
                if self.g.m.bridges.get(cid) is not bridge or bridge.closing or bridge.handoff or bridge.end_requested.is_set():return
                if generation!=self.generations.get(cid,0):continue
                # GPT-Live captions have no final-turn event. Allow both the
                # captions and raw incoming speech to settle; current() and the
                # provider dispatch guard still recheck authority and new audio.
                if time.monotonic()-getattr(bridge,'last_input_speech_at',0)<self.settle_seconds:continue
                if self.g.store.prior_call_write(cid):return
                pending=self.pending(cid)
                if pending['state']!='ready':
                    await self.g.voice_confirmation.release_answer(bridge)
                    if pending.get('phase')=='awaiting_answer':await self.clarify(cid)
                    if generation!=self.generations.get(cid,0):continue
                    return
                result=await self.book(cid)
                log_event('appointment_voice_workflow',call_id=cid,state=result['state'],reason=result.get('reason',''))
                if result['state'] not in ('confirmed','needs_consent'):
                    uncertain=result['state'] in ('uncertain','committing','needs_owner')
                    text=('The booking could not be verified. Do not claim success or absence, and do not create another event. Offer owner review.'
                        if uncertain else 'The appointment is not confirmed. Explain that the slot or authorization changed; collect a new request only if the caller wants one.')
                    uid=uuid5(UUID(cid),'voice-booking-state-'+str(result.get('operation_id',''))+'-'+result['state'])
                    await self.g.m.send_call_update(cid,CallUpdateRequest(mode='instructions',content=text,update_id=uid))
                if result['state']!='confirmed' and generation!=self.generations.get(cid,0):continue
                return
        except (ValueError,KeyError,RuntimeError) as exc:
            log_event('appointment_voice_workflow_stopped',call_id=cid,error_type=type(exc).__name__)

    async def close(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
