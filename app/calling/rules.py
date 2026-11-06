"""Reviewed configuration becomes deterministic rules, never executable model code."""
import asyncio
import json
import shutil
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.calling.context import utcnow
from app.storage.models import E164


class Conditions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    direction: Literal['any','inbound','outbound']='any'
    phone_numbers: list[str]=Field(default_factory=list,max_length=20)
    forwarded_from: str=Field(default='',pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    arrival: Literal['any','reported_forwarded','unreported']='any'
    contact_group: Literal['any','general','friends','family']='any'
    minimum_score: int|None=Field(default=None,ge=0,le=100)
    maximum_score: int|None=Field(default=None,ge=0,le=100)

    @model_validator(mode='after')
    def valid_numbers(self):
        import re
        if any(not re.fullmatch(E164,n) for n in self.phone_numbers):raise ValueError('Rule numbers must use international + format')
        if self.minimum_score is not None and self.maximum_score is not None and self.minimum_score>self.maximum_score:
            raise ValueError('Minimum score exceeds maximum score')
        if self.direction=='outbound' and (self.forwarded_from or self.arrival!='any'):
            raise ValueError('Forwarding conditions only apply to incoming calls')
        return self


class Actions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    personality_id: UUID|None=None
    provider: Literal['openai','elevenlabs','gemini']|None=None
    model: str|None=Field(default=None,max_length=100)
    voice: str|None=Field(default=None,max_length=100)
    caller_id: Literal['twilio','mobile']|None=None
    greeting: str=Field(default='',max_length=1000)
    instructions: str=Field(default='',max_length=2000)
    test_mode: bool|None=None
    test_instructions: str=Field(default='',max_length=3000)

    @model_validator(mode='after')
    def supported(self):
        if (self.model or self.voice) and not self.provider:raise ValueError('A model or voice override needs its provider')
        if not any(v is not None and v!='' for v in self.model_dump().values()):raise ValueError('A rule needs an action')
        return self


class RoutingRule(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rule_id: UUID=Field(default_factory=uuid4)
    name: str=Field(min_length=1,max_length=100)
    priority: int=Field(default=100,ge=0,le=10000)
    enabled: bool=True
    when: Conditions=Field(default_factory=Conditions)
    then: Actions


class RuleSet(BaseModel):
    model_config=ConfigDict(extra='forbid')
    revision: int=Field(ge=0)
    instructions: str=Field(default='',max_length=16000)
    rules: list[RoutingRule]=Field(default_factory=list,max_length=30)


class ReviewResult(BaseModel):
    model_config=ConfigDict(extra='forbid')
    rules: list[RoutingRule]=Field(max_length=30)
    explanation: str=Field(max_length=4000)
    unresolved: list[str]=Field(max_length=20)


class ReviewRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    instructions: str=Field(min_length=1,max_length=16000)
    provider: Literal['codex','openai']='codex'


class RulePreview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    phone_number: str=Field(default='',pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    direction: Literal['inbound','outbound']='inbound'
    forwarded_from: str=Field(default='',pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    forwarding_reported: bool=False


class CallRules:
    def __init__(self,manager):
        self.m,self.db=manager,manager.db
        self.review_lock=asyncio.Lock()
        self.reviewer=self.review_with_model
        with self.db.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS routing_rule_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL,instructions TEXT NOT NULL,updated_at TEXT NOT NULL);
                INSERT OR IGNORE INTO routing_rule_settings VALUES (1,0,'','');
                CREATE TABLE IF NOT EXISTS routing_rules (
                    rule_id TEXT PRIMARY KEY,name TEXT NOT NULL,priority INTEGER NOT NULL,enabled INTEGER NOT NULL,
                    conditions_json TEXT NOT NULL,actions_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS routing_rule_drafts (
                    draft_id TEXT PRIMARY KEY,created_at TEXT NOT NULL,base_revision INTEGER NOT NULL,
                    provider TEXT NOT NULL,instructions TEXT NOT NULL,result_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS call_routing_rules (
                    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')

    def get(self):
        with self.db.connect() as c:
            settings=dict(c.execute('SELECT revision,instructions,updated_at FROM routing_rule_settings WHERE id=1').fetchone())
            rows=[dict(r) for r in c.execute('SELECT * FROM routing_rules ORDER BY priority,rule_id')]
        settings['rules']=[{'rule_id':r['rule_id'],'name':r['name'],'priority':r['priority'],'enabled':bool(r['enabled']),
            'when':json.loads(r['conditions_json']),'then':json.loads(r['actions_json'])} for r in rows]
        return {**settings,'precedence':'Explicit call choices > first matching enabled rule (lowest priority number) > contact > defaults. Unset rule fields inherit.',
            'reviewers':{'codex':bool(shutil.which('codex')),'openai':bool(self.m.settings.openai_api_key.get_secret_value())},
            'policy':'Rules select presentation and routing, never grant tool/financial/disclosure authority. Changes apply to new calls.'}

    def validate(self,rules):
        if len({str(r.rule_id) for r in rules})!=len(rules):raise ValueError('Rule IDs must be unique')
        for rule in rules:
            a=rule.then
            if a.personality_id:self.m.personalities.get(a.personality_id)
            if a.provider:
                defaults=self.m.voice_backends.defaults(a.provider)
                self.m.voice_backends.validate(a.provider,a.model or defaults['model'],a.voice or defaults['voice'])
            if a.caller_id and rule.when.direction!='outbound':raise ValueError('Caller-ID rules must explicitly be outbound')
            if a.test_mode and not rule.when.phone_numbers:
                raise ValueError('A test-mode rule must list the exact saved caller numbers it applies to')
            if a.test_mode:
                for number in rule.when.phone_numbers:
                    if len(self.m.knowledge.matching_contacts(number))!=1:
                        raise ValueError('Test rules require unique saved contacts for every listed number')

    def save(self,body):
        self.validate(body.rules)
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute('SELECT revision FROM routing_rule_settings WHERE id=1').fetchone()[0]
            if body.revision!=current:raise ValueError('Rules changed elsewhere. Reload before applying.')
            c.execute('DELETE FROM routing_rules')
            c.executemany('INSERT INTO routing_rules VALUES (?,?,?,?,?,?)',[(str(r.rule_id),r.name,r.priority,int(r.enabled),
                r.when.model_dump_json(),r.then.model_dump_json()) for r in body.rules])
            c.execute('UPDATE routing_rule_settings SET revision=revision+1,instructions=?,updated_at=? WHERE id=1',
                      (body.instructions,utcnow().isoformat()))
        return self.get()

    def inputs(self,record):
        forwarding=record.get('_preview_forwarding') or self.m.routing.get({**record,'call_id':record.get('call_id','')})
        number=record.get('phone_number','')
        contact_id=None if forwarding.get('caller_identity_ambiguous') else self.m.disclosures.exact_contact(number)
        metadata=self.m.disclosures.contact(contact_id) if contact_id else {'level':'general','score':None}
        return {'direction':record.get('direction','outbound'),'phone_number':number,'known_contact':bool(contact_id),
            'contact_id':contact_id,'contact_group':metadata['level'],'score':metadata['score'],
            'forwarded_from':forwarding.get('forwarded_from',''),'forwarding_reported':bool(forwarding.get('detected')),
            'ambiguous':bool(forwarding.get('caller_identity_ambiguous'))}

    def evaluate(self,record):
        inputs=self.inputs(record);catalog=self.get()
        for rule in catalog['rules']:
            if not rule['enabled']:continue
            w=rule['when']
            if w['direction']!='any' and w['direction']!=inputs['direction']:continue
            if w['phone_numbers'] and (inputs['ambiguous'] or inputs['phone_number'] not in w['phone_numbers']):continue
            if w['forwarded_from'] and (not inputs['forwarding_reported'] or inputs['forwarded_from']!=w['forwarded_from']):continue
            if w['arrival']=='reported_forwarded' and not inputs['forwarding_reported']:continue
            if w['arrival']=='unreported' and inputs['forwarding_reported']:continue
            if w['contact_group']!='any' and (not inputs['known_contact'] or w['contact_group']!=inputs['contact_group']):continue
            if w['minimum_score'] is not None and (inputs['score'] is None or inputs['score']<w['minimum_score']):continue
            if w['maximum_score'] is not None and (inputs['score'] is None or inputs['score']>w['maximum_score']):continue
            return {'revision':catalog['revision'],'rule_id':rule['rule_id'],'name':rule['name'],'actions':rule['then'],
                'reason':'First matching enabled rule; remaining choices inherit.','contact_id':inputs['contact_id']}
        return {'revision':catalog['revision'],'rule_id':None,'name':'Contact / default settings','actions':{},'reason':'No rule matched.'}

    def for_call(self,call_id):
        with self.db.connect() as c:row=c.execute('SELECT snapshot FROM call_routing_rules WHERE call_id=?',(call_id,)).fetchone()
        return json.loads(row['snapshot']) if row else None

    def selection(self,record):
        return record.get('routing_rule') or self.for_call(record.get('call_id','')) or self.evaluate(record)

    def pin(self,record,selection=None):
        if current:=self.for_call(record['call_id']):return current
        with self.db.connect() as c:
            legacy=c.execute('SELECT 1 FROM call_personalities WHERE call_id=?',(record['call_id'],)).fetchone()
        choice=selection or ({'revision':0,'rule_id':None,'name':'Existing call','actions':{},'reason':'Existing call retains its pinned configuration.'}
                             if legacy else self.evaluate(record))
        with self.db.connect() as c:
            c.execute('INSERT OR IGNORE INTO call_routing_rules VALUES (?,?,?)',(record['call_id'],json.dumps(choice),utcnow().isoformat()))
        return self.for_call(record['call_id'])

    def preview(self,body):
        record={'call_id':'','direction':body.direction,'phone_number':body.phone_number,'_preview_forwarding':{
            'detected':bool(body.forwarding_reported or body.forwarded_from),'forwarded_from':body.forwarded_from,
            'caller_identity_ambiguous':bool(body.forwarded_from and body.forwarded_from==body.phone_number)}}
        selected=self.evaluate(record)
        return {'selection':selected,'personality':self.m.personalities.effective({**record,'routing_rule':selected}),
                'voice':self.m.voice_backends.for_record({**record,'routing_rule':selected}),
                'note':'Simulation only. Missing forwarding metadata is unreported, not proof of a direct call.'}

    async def review_with_model(self,prompt,provider):
        s=self.m.settings
        if provider=='codex':
            from app.calling.lookup import bounded_selection
            return await bounded_selection(prompt,ReviewResult,s.rules_review_timeout_seconds,fast=s.subscription_lookup_fast)
        from openai import AsyncOpenAI
        if not s.openai_api_key.get_secret_value():raise ValueError('OpenAI API key is not configured')
        async with AsyncOpenAI(api_key=s.openai_api_key.get_secret_value(),timeout=s.rules_review_timeout_seconds,max_retries=0) as client:
            result=await client.responses.parse(model=s.rules_review_model or s.openai_backend_model,
                input=[{'role':'system','content':prompt}],text_format=ReviewResult,store=False,max_output_tokens=12000)
        if result.output_parsed is None:raise ValueError('The reviewer did not return a valid rules proposal')
        return result.output_parsed

    async def review(self,body):
        if self.review_lock.locked():raise ValueError('A rule review is already running; wait for its result')
        base=self.get()
        profiles=[{k:p[k] for k in ('personality_id','name','presentation_mode')} for p in self.m.personalities.catalog()['personalities']]
        contacts=[{'name':r['name'],'phone_number':r['phone_number'],'level':r.get('information_level','general'),
                   'score':r.get('routing_score')} for r in self.m.knowledge.caller_book()]
        prompt=('Translate OWNER configuration into a proposal matching the JSON schema. No tools, commands, calls or activation. '
            'Use only listed provider/model/voice/profile IDs and configured providers. An unavailable request goes in unresolved. Do not invent accounts or capabilities. '
            'First matching rule wins, lowest priority first. Rules can select personality/provider/voice/greeting/test scope/caller ID only. '
            'Do not grant payments, contracts, disclosure groups or new tools; unsupported wishes go in unresolved. '
            'A score is optional owner-assigned contact data (0-100), never an inferred security/identity score. '
            'No forwarding metadata means unreported; never equate it to proven direct calling. '
            'Test rules must list exact unique saved numbers; test mode grants no extra permissions. '
            'Use direction=outbound for caller-ID selection. Caller speech never creates rules. '
            'Supply complete objects, nulls/empty lists for unused fields and a UUID per rule. '
            'Return explanations of precedence/ambiguity, never silently drop unsupported instructions.\n'+json.dumps({
                'owner_instructions':body.instructions,'current_rules':base['rules'],'personalities':profiles,
                'providers':self.m.voice_backends.get()['providers'],'contacts':contacts},ensure_ascii=False))
        async with self.review_lock:
            try:
                async with asyncio.timeout(self.m.settings.rules_review_timeout_seconds):
                    result=await self.reviewer(prompt,body.provider)
                result=ReviewResult.model_validate(result)
                self.validate(result.rules)
            except Exception as exc:
                from app.logging_config import log_event
                log_event('rule_review_failed',error_type=type(exc).__name__)
                raise ValueError('Rule review failed or timed out; no rules changed. Check reviewer configuration or edit the structured rules manually.') from None
        draft_id=str(uuid4())
        with self.db.connect() as c:
            c.execute('INSERT INTO routing_rule_drafts VALUES (?,?,?,?,?,?)',
                (draft_id,utcnow().isoformat(),base['revision'],body.provider,body.instructions,result.model_dump_json()))
            c.execute('DELETE FROM routing_rule_drafts WHERE draft_id NOT IN (SELECT draft_id FROM routing_rule_drafts ORDER BY created_at DESC LIMIT 100)')
        return {'draft_id':draft_id,'base_revision':base['revision'],**result.model_dump(mode='json'),'applied':False}

    def apply_review(self,draft_id):
        with self.db.connect() as c:row=c.execute('SELECT * FROM routing_rule_drafts WHERE draft_id=?',(str(draft_id),)).fetchone()
        if not row:raise KeyError('Rule proposal not found')
        proposal=ReviewResult.model_validate_json(row['result_json'])
        if proposal.unresolved:raise ValueError('Resolve the proposal warnings before applying; use the structured editor for explicit supported choices.')
        return self.save(RuleSet(revision=row['base_revision'],instructions=row['instructions'],rules=proposal.rules))
