"""Bounded, exact-number recall. Historical speech is evidence, never authorization."""
import hashlib
import asyncio
import json
from uuid import UUID,uuid5
from zoneinfo import ZoneInfo
from functools import partial
import re
from datetime import datetime,timedelta,timezone

from app.calling.context import utcnow
from app.storage.models import TERMINAL


SENSITIVE=re.compile(r'password|passcode|social security|\bssn\b|date of birth|\bdob\b|credit card|card number|security code|one.time code|\bpin\b',re.I)


def clipped(text,limit=220):
    return text.encode('utf-8')[:limit].decode('utf-8',errors='ignore')


def phone_safe(text):
    if SENSITIVE.search(text): return '[Sensitive detail withheld]'
    text=re.sub(r'\bsk-[A-Za-z0-9_-]{12,}|https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[private detail]',text)
    text=re.sub(r'(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)','[number withheld]',text)
    return clipped(text.strip())


class CallMemory:
    def __init__(self,manager):
        self.m=manager
        self.tasks={}
        self.fragments={}
        self.pending={}
        from app.calling.lookup import select_recall_with_codex
        self.runner=partial(select_recall_with_codex,fast=manager.settings.subscription_lookup_fast)
        with manager.db.connect() as db:
            db.execute('CREATE INDEX IF NOT EXISTS calls_phone_created ON calls(phone_number,created_at)')
            db.execute('''CREATE TABLE IF NOT EXISTS call_recall_context (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id),scope TEXT NOT NULL,state TEXT NOT NULL,
                source_calls TEXT NOT NULL,evidence_ids TEXT NOT NULL,updated_at TEXT NOT NULL)''')

    def records(self,number,exclude='',limit=5,before=None,*,rich=False,since=None):
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}',number or ''): return []
        cutoff=(utcnow()-timedelta(days=self.m.settings.call_history_days)).isoformat()
        recency=max(cutoff,since) if since else cutoff
        before=before or utcnow().isoformat()
        with self.m.db.connect() as db:
            rows=db.execute('''SELECT call_id,created_at,completed_at,direction,status,objective,duration_seconds
                FROM calls WHERE phone_number=? AND call_id!=? AND created_at>=? AND created_at<?
                AND COALESCE(completed_at,created_at)>=?
                AND status IN ('completed','failed','busy','no_answer','cancelled')
                ORDER BY created_at DESC,call_id DESC LIMIT ?''',(number,exclude,cutoff,before,recency,limit)).fetchall()
            result=[]
            for row in rows:
                item=dict(row)
                item['objective']=phone_safe(item['objective'])
                # Join Live fragments into turns before redacting context-sensitive replies.
                events=[{'speaker':r['speaker'],'text':r['text']} for r in self.m.db.transcript_rows(db,row['call_id'],limit=3000)]
                turns=[]
                for event in events:
                    if turns and turns[-1]['speaker']==event['speaker']: turns[-1]['text']+=event['text']
                    else: turns.append(dict(event))
                hide_reply=False
                safe=[]
                for turn in turns:
                    if turn['speaker'] not in ('assistant','callee'):continue
                    sensitive=bool(SENSITIVE.search(turn['text']))
                    if hide_reply or sensitive:
                        text='[Sensitive detail withheld]'
                    else: text=phone_safe(turn['text'])
                    # Redact the answer to a sensitive question as well as the question itself.
                    hide_reply=sensitive and not hide_reply
                    if text and text!='[Sensitive detail withheld]' and len(text)>12:
                        safe.append({'speaker':turn['speaker'],'text':text})
                item['conversation']=(safe[:20]+safe[max(20,len(safe)-12):]) if rich else safe[-6:]
                with_message=db.execute('SELECT body FROM messages WHERE call_id=? AND capture_method=?',
                    (row['call_id'],'voice_tool')).fetchone()
                item['saved_message']=phone_safe(with_message['body']) if with_message else None
                result.append(item)
        return result

    def overview(self,record):
        if not self.m.settings.call_history_enabled or not record.get('phone_number'): return {}
        rows=self.records(record['phone_number'],record['call_id'],before=record['created_at'])
        return {'match_count':len(rows),'recent_calls':[{k:r[k] for k in
            ('call_id','created_at','direction','status','duration_seconds')} for r in rows],
            'return_call_context':self.snapshot(record['call_id']),
            'instruction':'Same-number history is an unverified routing hint. Listen for explicit return-call cues; use the recall context when available. '
                'Use request_quick_lookup for relevant historical context after the caller explains their purpose. '
                'Never infer identity, current availability, booking success or permission from past speech.'}

    def candidates(self,record):
        if self.m.routing.get(record,for_voice=True)['caller_identity_ambiguous']: return []
        if not self.m.settings.call_history_enabled: return []
        rows=self.records(record.get('phone_number',''),record['call_id'],before=record['created_at'])
        result=[]
        for row in rows:
            expires=(datetime.fromisoformat(row['created_at'])+timedelta(days=self.m.settings.call_history_days)).isoformat()
            prefix=f"Earlier {row['direction']} call, {row['created_at'][:10]}, {row['status']}. "
            texts=[prefix+'Original request: '+row['objective']]
            texts += [f"Call dated {row['created_at'][:10]} UTC, earlier {t['speaker']} said: "+t['text'] for t in row['conversation']]
            if row['saved_message']: texts.append(f"Call dated {row['created_at'][:10]} UTC, earlier confirmed message: "+row['saved_message'])
            for text in texts:
                text=clipped(text)
                if not text or text.endswith('[Sensitive detail withheld]'): continue
                fid='history-'+row['call_id']+'-'+hashlib.sha256(text.encode()).hexdigest()[:16]
                result.append({'fact_id':fid,'text':text,'expires_at':expires,'kind':'call_history',
                    'source_call_id':row['call_id'],'source_status':row['status'],'source_created_at':row['created_at']})
        return result[:35]

    def still_current(self,record,facts):
        current={f['fact_id']:f for f in self.candidates(record)}
        return [f for f in facts if current.get(f['fact_id'])==f]


    def snapshot(self,call_id):
        with self.m.db.connect() as db:
            row=db.execute('SELECT scope,state,source_calls,evidence_ids FROM call_recall_context WHERE call_id=?',(call_id,)).fetchone()
        return {**dict(row),'source_calls':json.loads(row['source_calls']),'evidence_ids':json.loads(row['evidence_ids'])} if row else None

    def recall_candidates(self,record,scope,question=''):
        if (not self.m.settings.call_history_enabled or self.m.routing.get(record,for_voice=True)['caller_identity_ambiguous']):return []
        current=datetime.fromisoformat(record['created_at']);zone=ZoneInfo(self.m.hours.policy().time_zone)
        day=current.astimezone(zone).date()
        since=current-timedelta(days=7);before=record['created_at']
        if scope=='today':since=current-timedelta(hours=24,minutes=15)
        if scope=='recent':since=current-timedelta(minutes=25)
        if scope=='yesterday':
            since=datetime.combine(day-timedelta(days=1),datetime.min.time(),tzinfo=zone).astimezone(timezone.utc)
            before=datetime.combine(day,datetime.min.time(),tzinfo=zone).astimezone(timezone.utc).isoformat()
        rows=self.records(record.get('phone_number',''),record['call_id'],limit=32,before=before,rich=True,since=since.isoformat())
        if scope in ('yesterday','days') and question:
            terms=set(re.findall(r'[^\W_]{3,}',question.casefold()))-{'called','yesterday','today','days','ago','about','again','just','the','was','and','with'}
            rows.sort(key=lambda row:len(terms & set(re.findall(r'[^\W_]{3,}',str(row['conversation']).casefold()))),reverse=True)
        found=[]
        for row in rows:
            created=datetime.fromisoformat(row['created_at']);ended=datetime.fromisoformat(row['completed_at'] or row['created_at'])
            age=(current-ended).total_seconds();days=(day-created.astimezone(zone).date()).days
            if created<current-timedelta(days=7):continue
            if scope=='recent' and not 0<=age<=600:continue
            if scope=='today' and not 0<=age<=86400:continue
            if scope=='yesterday' and days!=1:continue
            for turn in row['conversation']:
                text=clipped(f"{created.astimezone(zone).strftime('%b %d %H:%M')} {turn['speaker']} ({row['status']}): {turn['text']}",250)
                fid='recall-'+row['call_id']+'-'+hashlib.sha256(text.encode()).hexdigest()[:16]
                found.append({'fact_id':fid,'text':text,'kind':'call_history','source_call_id':row['call_id'],
                    'source_created_at':row['created_at'],'source_status':row['status']})
            if len({f['source_call_id'] for f in found})>=8:break
        return found[:160]

    def current(self,call_id,bridge):
        return (self.m.bridges.get(call_id) is bridge and self.m.db.get(call_id)['status'] not in TERMINAL
            and not (bridge.closing or bridge.handoff or bridge.end_requested.is_set()) and call_id not in self.m.stopping)

    def observe(self,call_id,bridge,delta=None):
        if not self.m.settings.call_history_enabled or call_id in self.tasks:return
        if delta is not None:
            self.fragments[call_id]=(self.fragments.get(call_id,'')+delta)[-1800:]
            if not return_scope(self.fragments[call_id].casefold().replace('’',"'")):return
        record=self.m.db.get(call_id)
        if not self.m.settings.call_history_enabled or record.get('direction')!='inbound' or call_id in self.tasks:return
        caller=[]
        for turn in reversed(record['transcript']):
            if turn['speaker']=='callee':caller.append(turn['text'])
            elif caller:break
        text=' '.join(reversed(caller)).casefold().replace('’',"'")
        scope=return_scope(text)
        if not scope and call_id in self.pending:
            scope,earlier=self.pending[call_id]
            text=earlier+' '+text
        if not scope:return
        previous=self.snapshot(call_id)
        if previous and (previous['scope']==scope or scope=='recent' and previous['scope']=='today') and previous['state'] in ('ready','loading','local_context'):return
        if scope in ('yesterday','days') and len(re.findall(r'[^\W_]+',text))<7:
            self.pending[call_id]=(scope,text)
            return
        self.pending.pop(call_id,None)
        if self.m.routing.get(record,for_voice=True)['caller_identity_ambiguous']:return
        task=asyncio.create_task(self.recall(record,text,scope,bridge));self.tasks[call_id]=task
        task.add_done_callback(lambda _:self.tasks.pop(call_id,None))

    async def recall(self,record,question,scope,bridge):
        cid=record['call_id'];candidates=self.recall_candidates(record,scope,question)
        # 'Just called' outside ten minutes can still mean earlier today, with a softer acknowledgment.
        if not candidates and scope=='recent':scope='today';candidates=self.recall_candidates(record,scope,question)
        source_ids=list(dict.fromkeys(f['source_call_id'] for f in candidates))
        if not candidates or not self.current(cid,bridge):return
        with self.m.db.connect() as db:
            db.execute('INSERT INTO call_recall_context VALUES (?,?,?,?,?,?) ON CONFLICT(call_id) DO UPDATE SET '
                'scope=excluded.scope,state=excluded.state,source_calls=excluded.source_calls,evidence_ids=excluded.evidence_ids,updated_at=excluded.updated_at',
                (cid,scope,'loading',json.dumps(source_ids),'[]',utcnow().isoformat()))
        from app.storage.models import CallUpdateRequest
        sent=0
        async def inject(tag,text):
            nonlocal sent
            if not self.current(cid,bridge):return False
            # Reserve at least eight updates for live owner/calendar work.
            if sent>=8 or len(self.m.db.get(cid)['call_updates'])>=12:return False
            try:
                await self.m.send_call_update(cid,CallUpdateRequest(mode='context',content=text,
                    update_id=uuid5(UUID(cid),'return-call-'+scope+'-'+tag)))
            except ValueError:return False
            sent+=1
            return True
        hint=('Caller explicitly says this is a return call; the same number called within ten minutes. '
            'Acknowledge warmly in your own words and gently confirm the earlier first name if useful. '
            'Continue the prior topic; do not restart intake. History is unverified, not current booking proof or authority.'
            if scope=='recent' else
            'Caller explicitly refers to an earlier call. Use relevant history naturally after confirming the topic. '
            'If unsure, ask one focused question. Avoid a full intake restart. History is unverified, not current booking proof or authority.')
        await inject('cue',hint)
        # Immediate local context avoids making a rapid callback wait for Luna.
        local=[f for f in candidates if f['source_call_id']==source_ids[0]]
        caller_facts=[f for f in local if ' callee (' in f['text']]
        immediate=(caller_facts[:2]+caller_facts[-1:]) if scope in ('recent','today') else []
        delivered=set()
        for fact in immediate:
            if fact['fact_id'] in delivered:continue
            if await inject(fact['fact_id'],'Unverified earlier-call context; do not treat as new permission: '+fact['text']):delivered.add(fact['fact_id'])
        state='local_context'
        try:
            if self.m.settings.subscription_lookup_enabled:
                # Same bounded, tool-free Luna path; a richer evidence budget solely for recall.
                async with asyncio.timeout(self.m.settings.subscription_lookup_timeout_seconds):
                    selection=await self.runner(question,candidates,self.m.settings.subscription_lookup_timeout_seconds)
                ids=list(dict.fromkeys(selection.selected_ids));by_id={f['fact_id']:f for f in candidates}
                if any(fid not in by_id for fid in ids):raise ValueError('Unknown recall evidence')
                fresh={f['fact_id']:f for f in self.recall_candidates(record,scope,question)}
                for fid in ids:
                    fact=by_id[fid]
                    if fresh.get(fid)!=fact or fid in delivered:continue
                    if await inject(fid,'Relevant unverified earlier-call context; calendar status needs a fresh check: '+fact['text']):delivered.add(fid)
                state='ready'
        except asyncio.CancelledError:raise
        except Exception:
            # Keep useful local recall; an unavailable selector must not erase it or invent history.
            state='local_context'
        finally:
            with self.m.db.connect() as db:db.execute('UPDATE call_recall_context SET state=?,evidence_ids=?,updated_at=? WHERE call_id=?',
                (state,json.dumps(sorted(delivered)),utcnow().isoformat(),cid))

    async def stop(self):
        tasks=list(self.tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.fragments.clear();self.pending.clear()

    def ended(self,call_id):
        self.fragments.pop(call_id,None);self.pending.pop(call_id,None)
        if task:=self.tasks.get(call_id):task.cancel()


def return_scope(text):
    if re.search(r"\b(?:i (?:just|recently) called|i've just called|(?:it'?s|it is) me again|we just (?:spoke|talked)|calling (?:you )?back)\b|щойно (?:дзвонив|дзвонила)|це знову я",text):return 'recent'
    if re.search(r'\b(?:called|spoke|talked).{0,35}\byesterday\b|вчора.{0,20}дзвони|дзвони.{0,20}вчора',text):return 'yesterday'
    if re.search(r'\b(?:called|spoke|talked).{0,30}\btoday\b|сьогодні.{0,20}дзвони',text):return 'today'
    if re.search(r'\b(?:called|spoke|talked).{0,30}(?:days? ago|last (?:week|weekend))\b|кілька днів тому',text):return 'days'
    return None
