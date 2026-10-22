"""Call-bound booking saga. Provider dictionaries and SDKs never enter this layer."""
import asyncio
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from app.appointments.contracts import Appointment,CalendarEvent,Coverage,Interval,ProviderFault,WriteNotAuthorized
from app.appointments.registry import Registry,appointment,event_identity,payload_hash
from app.appointments.policy import BookingPolicy


@dataclass(frozen=True)
class CallAuthority:
    active: bool
    deadline: datetime
    revision: str
    booking_allowed: bool


@dataclass(frozen=True)
class ConsentEvidence:
    payload_hash: str
    caller_revision: str
    readback_observed: bool
    affirmative: bool


class BookingService:
    def __init__(self,registry,provider,authority,consent,policy=None,clock=None):
        self.store,self.provider=registry,provider
        self.authority,self.consent=authority,consent
        self.policy=policy or BookingPolicy();self.clock=clock or (lambda:datetime.now(timezone.utc))
        self.lock=asyncio.Lock()

    def current(self,draft):
        value=self.authority(draft['call_id'],draft['request_id'])
        if not value.active or not value.booking_allowed or value.deadline<=self.clock():
            raise ValueError('Call/request has no current booking authority')
        return value

    def prepare(self,call_id,request_id,value:Appointment):
        self.current({'call_id':call_id,'request_id':request_id})
        return self.store.draft(call_id,request_id,value)

    def interval(self,draft):
        i=appointment(draft).interval
        return Interval(i.start-timedelta(minutes=self.policy.buffer_before),i.end+timedelta(minutes=self.policy.buffer_after),i.zone)

    async def check(self,call_id,draft_id):
        draft=self.bound(call_id,draft_id);authority=self.current(draft);now=self.clock()
        try:
            if not (self.provider.capabilities.exact_reads and self.provider.capabilities.recurrence_expansion):raise ProviderFault('unsupported')
            async with asyncio.timeout(max(.001,min(6,(authority.deadline-self.clock()).total_seconds()))):
                with self.store.measure('calendar_check'):coverage=await self.provider.read_interval(self.interval(draft))
            decision,reason=self.policy.decide(appointment(draft).interval,coverage,now)
        except (ProviderFault,TimeoutError) as exc:
            reason=exc.code if isinstance(exc,ProviderFault) else 'deadline_budget'
            coverage=Coverage(self.interval(draft),now,now,False,reason=reason);decision='unknown'
        self.current(draft)
        expires=min(now+timedelta(seconds=self.policy.check_ttl_seconds),authority.deadline,coverage.valid_until)
        result=self.store.check(draft,self.policy.version,decision,reason,now,expires,coverage.complete)
        return {**result,'reservation':False,'verified_alternatives':[]}

    def bound(self,call_id,draft_id):
        draft=self.store.get('appointment_drafts','draft_id',draft_id)
        if draft['call_id']!=call_id:raise ValueError('Call binding mismatch')
        return draft

    def verify_consent(self,draft,consent_id):
        authority=self.current(draft);evidence=self.consent(draft,consent_id)
        if not (evidence.affirmative and evidence.readback_observed and evidence.payload_hash==draft['payload_hash']
                and evidence.caller_revision==authority.revision):
            raise ValueError('Fresh caller agreement after complete readback is required')
        return authority

    async def book(self,call_id,draft_id,check_id,consent_id,idempotency_key):
        if not idempotency_key or len(idempotency_key)>100:raise ValueError('Use a stable bounded idempotency key')
        async with self.lock:
            draft=self.bound(call_id,draft_id)
            prior=self.store.prior_call_write(call_id)
            if prior and prior['draft_id']!=draft_id:
                return {'operation_id':prior['operation_id'],'state':'needs_owner','reason':'existing_call_write_requires_reconciliation'}
            try:old=self.store.get('booking_operations','draft_id',draft_id)
            except KeyError:old=None
            if old:
                if old['payload_hash']!=draft['payload_hash']:raise ValueError('Operation payload changed')
                return await self.status(call_id,old['operation_id'])
            authorization=self.verify_consent(draft,consent_id)
            check=self.store.get('appointment_checks','check_id',check_id)
            if (check['draft_id']!=draft_id or check['draft_revision']!=draft['revision'] or check['decision']!='available'
                    or check['policy_version']!=self.policy.version or datetime.fromisoformat(check['expires_at'])<=self.clock()):
                raise ValueError('An unexpired exact check for this revision is required')
            if not self.provider.capabilities.safe_booking():return {'state':'needs_owner','reason':'unsupported_guarantees'}
            intended=self.provider.intended_identity(draft_id)
            operation,created=self.store.intent(draft,idempotency_key,intended)
            if not created:return await self.status(call_id,operation['operation_id'])
            oid=operation['operation_id']
            try:
                async with asyncio.timeout(max(.001,min(6,(authorization.deadline-self.clock()).total_seconds()))):
                    with self.store.measure('precommit_check'):coverage=await self.provider.read_interval(self.interval(draft))
                decision,reason=self.policy.decide(appointment(draft).interval,coverage,self.clock())
                if decision!='available':return self.store.transition(oid,'conflict' if decision=='busy' else 'needs_owner','recheck',reason)
                latest=self.verify_consent(draft,consent_id)
                if latest.revision!=authorization.revision:raise ValueError('Caller interrupted during calendar I/O')
            except (ProviderFault,ValueError,TimeoutError):return self.store.transition(oid,'needs_owner','recheck','authority_or_coverage_changed')
            self.store.transition(oid,'committing','write_sent',write_attempted=True)
            def before_dispatch():
                try:
                    if self.verify_consent(draft,consent_id).revision!=authorization.revision:raise ValueError('Changed consent')
                except ValueError:raise WriteNotAuthorized() from None
            try:
                async with asyncio.timeout(max(.001,min(5,(authorization.deadline-self.clock()).total_seconds()))):
                    with self.store.measure('provider_commit'):await self.provider.create(intended,appointment(draft),authorize=before_dispatch)
            except WriteNotAuthorized:
                return self.store.transition(oid,'needs_owner','write_not_started','authority_changed_before_dispatch',write_attempted=False)
            except asyncio.CancelledError:
                self.store.transition(oid,'uncertain','reconcile','write_interrupted');raise
            except Exception:
                self.store.transition(oid,'uncertain','reconcile','provider_write_unconfirmed')
            return await self.reconcile(oid)

    async def reconcile(self,oid):
        try:
            async with asyncio.timeout(6):return await self._reconcile(oid)
        except TimeoutError:return self.store.transition(oid,'uncertain','reconcile','reconcile_budget')

    async def _reconcile(self,oid):
        operation=self.store.get('booking_operations','operation_id',oid)
        if not operation['write_attempted']:return operation
        draft=self.store.get('appointment_drafts','draft_id',operation['draft_id']);expected=appointment(draft)
        try:
            with self.store.measure('independent_readback'):event=await self.provider.read_event(event_identity(operation))
            if event is None:return self.store.transition(oid,'uncertain','reconcile','identity_not_yet_visible')
            if (event.identity!=event_identity(operation) or event.state=='cancelled' or not event.interval
                    or (event.interval.start,event.interval.end,event.interval.zone)!=(expected.interval.start,expected.interval.end,expected.interval.zone)
                    or event.location!=expected.location or event.participant_first!=expected.first_name
                    or event.participant_last!=expected.last_name or event.purpose!=expected.purpose or event.disclosure!='ordinary'
                    or event.ownership!='phone-assistant' or not event.revision):
                return self.store.transition(oid,'needs_owner','readback','persisted_fields_differ')
            coverage=await self.provider.read_interval(self.interval(draft),exclude=event.identity)
            decision,reason=self.policy.decide(expected.interval,coverage,self.clock(),exclude=event.identity)
            if decision!='available':return self.store.transition(oid,'needs_owner','external_race',reason)
            now=self.clock()
            with self.store.measure('registry_persistence'):self.store.confirmed(operation,draft,event,now,now+timedelta(minutes=5))
        except ProviderFault as exc:return self.store.transition(oid,'uncertain','reconcile',exc.code)
        except Exception:
            # Event is retained. Repair registry on status/recovery; never compensate by deletion.
            try:self.store.transition(oid,'uncertain','registry_repair','registry_unavailable')
            except Exception:pass
            return {'operation_id':oid,'state':'uncertain','reason':'registry_repair_required'}
        return self.store.get('booking_operations','operation_id',oid)

    async def status(self,call_id,oid):
        row=self.store.get('booking_operations','operation_id',oid)
        if row['call_id']!=call_id:raise ValueError('Call binding mismatch')
        if row['state'] in ('uncertain','committing') and row['write_attempted']:return await self.reconcile(oid)
        return row

    async def own_appointment(self,first,last,*,contradictory=False):
        if contradictory or not first.strip() or not last.strip():return {'status':'needs_owner'}
        result=self.store.own_appointment(first,last,self.clock())
        if result['status'] not in ('match','stale') or 'appointment' not in result:return result
        row=result['appointment']
        try:
            async with asyncio.timeout(6):
                event=await self.provider.read_event(event_identity(row))
                coverage=await self.provider.read_interval(Interval(datetime.fromisoformat(row['start']),
                    datetime.fromisoformat(row['end']),row['zone']),exclude=event_identity(row))
        except (ProviderFault,TimeoutError):return {'status':'stale'}
        if (not event or event.state=='cancelled' or event.revision!=row['source_revision']
                or event.identity!=event_identity(row) or not event.interval or event.disclosure!='ordinary'
                or event.participant_first!=row['first_name'] or event.participant_last!=row['last_name']
                or event.interval.start!=datetime.fromisoformat(row['start']) or event.interval.end!=datetime.fromisoformat(row['end'])
                or event.interval.zone!=row['zone'] or event.location!=row['location']
                or not coverage.complete or not coverage.checked_at<=self.clock()<coverage.valid_until):
            return {'status':'stale'}
        return {'status':'match',**{k:row[k] for k in ('start','end','zone','location')},
            'duration_seconds':int((event.interval.end-event.interval.start).total_seconds())}
