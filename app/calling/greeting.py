"""Best-effort subscription Luna preparation during the existing pre-answer pause."""
import asyncio
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from app.calling.context import utcnow
from app.logging_config import log_event
from app.storage.models import TERMINAL


class GreetingPreparation:
    def __init__(self,manager):
        self.m=manager
        self.cache={}
        self.started=set()

    def candidates(self,record):
        m=self.m
        result=m.knowledge.search('',caller_number=record['phone_number'])['facts'] if record['inbound_mode']=='concierge' else []
        result+=m.memory.candidates(record)
        matches=m.knowledge.matching_contacts(record['phone_number'])
        if len(matches)==1 and m.settings.caller_id_trust_saved_contacts:
            text='Known caller: '+matches[0]['name']+'. Preferred language: '+(matches[0]['preferred_language'] or 'not set')+'.'
            text+=' Spoken name: '+(matches[0].get('preferred_name') or matches[0]['name'])+\
                '. Conversation style: '+matches[0].get('conversation_style','formal')+'.'
            result.append({'fact_id':'identity-'+hashlib.sha256(text.encode()).hexdigest(),
                'text':text,'expires_at':(utcnow()+timedelta(hours=1)).isoformat(),'kind':'caller_identity'})
        for note in m.context.for_call(record).get('expected_calls',[]) if record['inbound_mode']=='concierge' else []:
            if len(note['context'])<=240:
                result.append({'fact_id':'expected-'+note['context_id'],'text':note['context'],
                    'expires_at':note['expires_at'],'kind':'anticipated_call'})
        return result[:50]

    def start(self,call_id):
        m=self.m
        delay=m.settings.inbound_answer_delay_seconds
        if not m.settings.subscription_lookup_enabled or delay<2 or call_id in self.started: return
        self.started.add(call_id)
        record=m.db.get(call_id)
        if record['status'] in TERMINAL or m.lookup.tasks: return
        candidates=self.candidates(record)
        if not candidates: return
        with m.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            hourly=db.execute('SELECT count(*) FROM concierge_lookups WHERE created_at>?',
                ((utcnow()-timedelta(hours=1)).isoformat(),)).fetchone()[0]
            if hourly>=20: return
            rid=str(uuid4())
            db.execute('INSERT INTO concierge_lookups(request_id,call_id,question,status,created_at) VALUES (?,?,?,?,?)',
                (rid,call_id,'Prepare current caller context during pre-answer ringing.','pending',utcnow().isoformat()))
        task=asyncio.create_task(self.work(rid,record,candidates,min(delay-.75,m.settings.subscription_lookup_timeout_seconds)))
        m.lookup.tasks[rid]=task
        task.add_done_callback(lambda _:m.lookup.tasks.pop(rid,None))

    async def work(self,rid,record,candidates,timeout):
        status='unavailable';selected=[];error=None
        try:
            async with asyncio.timeout(timeout):
                result=await self.m.lookup.runner(
                    'Prepare a personalized incoming-call greeting for the owner assistant. Select current known-caller '
                    'identity/language or relevant anticipated-call facts. Historical excerpts are INTERNAL background '
                    'only and must not be volunteered in a greeting. No actions or new facts.',candidates,timeout)
            if self.m.db.get(record['call_id'])['status'] in TERMINAL: return
            ids=list(dict.fromkeys(result.selected_ids))
            selected=[f for f in candidates if f['fact_id'] in ids]
            if len(selected)!=len(ids): raise ValueError('Unknown preparation evidence')
            # Identity evidence is stable by text; temporary expiry must not change its identity.
            fresh={f['fact_id']:f for f in self.candidates(record)}
            selected=[f for f in selected if f['fact_id'] in fresh and fresh[f['fact_id']]['text']==f['text']]
            self.cache[record['call_id']]=selected
            status='answered' if selected else 'no_matching_facts'
        except asyncio.CancelledError:
            status='cancelled';raise
        except Exception as exc:
            error='timeout' if isinstance(exc,TimeoutError) else 'preparation_failed'
        finally:
            with self.m.db.connect() as db:
                db.execute('UPDATE concierge_lookups SET status=?,completed_at=?,evidence_ids=?,delivery_status=?,error_code=? WHERE request_id=?',
                    (status,utcnow().isoformat(),json.dumps([f['fact_id'] for f in selected]),
                     'startup_context' if status=='answered' else 'not_sent',error,rid))
            log_event('incoming_greeting_prepared',call_id=record['call_id'],status=status,error_code=error)

    def snapshot(self,record):
        chosen=self.cache.get(record['call_id'],[])
        fresh={f['fact_id']:f for f in self.candidates(record)} if chosen else {}
        return [{'text':f['text'],'kind':f.get('kind','approved_fact')} for f in chosen
                if f['fact_id'] in fresh and fresh[f['fact_id']]['text']==f['text']]

    def ended(self,call_id):
        self.cache.pop(call_id,None)
        self.started.discard(call_id)
