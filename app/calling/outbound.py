"""Fixed owner caller IDs, reusable portal brief, and one-shot outbound requests."""
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OutboundDefaults(BaseModel):
    model_config = ConfigDict(extra='forbid')
    caller_id: Literal['twilio', 'mobile'] = 'twilio'
    objective: str = Field(default='', max_length=4000)
    context: str = Field(default='', max_length=8000)
    constraints: list[str] = Field(default_factory=list, max_length=30)
    max_duration_minutes: int = Field(default=15, ge=1, le=15)
    ivr_mode: bool = False
    revision: int = Field(ge=1)


class OutboundCalls:
    def __init__(self, manager):
        self.m, self.db = manager, manager.db
        with self.db.connect() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS outbound_defaults (
                id INTEGER PRIMARY KEY CHECK(id=1),caller_id TEXT NOT NULL CHECK(caller_id IN ('twilio','mobile')),
                objective TEXT NOT NULL,context TEXT NOT NULL,constraints_json TEXT NOT NULL,
                max_duration_minutes INTEGER NOT NULL,ivr_mode INTEGER NOT NULL,revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO outbound_defaults VALUES (1,'twilio','','','[]',15,0,1);
                CREATE TABLE IF NOT EXISTS call_origination (
                    call_id TEXT PRIMARY KEY REFERENCES calls(call_id),request_id TEXT UNIQUE,request_hash TEXT NOT NULL,
                    caller_id TEXT NOT NULL CHECK(caller_id IN ('twilio','mobile')),from_number TEXT NOT NULL);
            ''')

    def mobile(self):
        return self.m.settings.outbound_mobile_number or self.m.settings.owner_transfer_number

    def get(self):
        with self.db.connect() as c:
            row=dict(c.execute('SELECT * FROM outbound_defaults WHERE id=1').fetchone())
        row.pop('id');row['constraints']=json.loads(row.pop('constraints_json'));row['ivr_mode']=bool(row['ivr_mode'])
        s=self.m.settings
        return {'defaults':row,'caller_ids':[
            {'id':'twilio','label':'Office / Twilio number','number':s.twilio_phone_number,'available':bool(s.twilio_phone_number)},
            {'id':'mobile','label':'My mobile','number':self.mobile(),'available':bool(self.mobile())}],
            'note':'Mobile caller ID is checked against this Twilio account before dialing. Calls still use Twilio; '
                   'callbacks to the displayed mobile reach that mobile. Voice, personality and recording use existing contact/default settings.'}

    async def save(self, body):
        await self.resolve(body.caller_id)
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT revision FROM outbound_defaults WHERE id=1').fetchone()[0]
            if body.revision != old:
                raise ValueError('Outbound defaults changed elsewhere. Reload before saving.')
            c.execute('UPDATE outbound_defaults SET caller_id=?,objective=?,context=?,constraints_json=?, '
                      'max_duration_minutes=?,ivr_mode=?,revision=revision+1 WHERE id=1',
                (body.caller_id,body.objective,body.context,json.dumps(body.constraints),body.max_duration_minutes,int(body.ivr_mode)))
        return self.get()

    async def resolve(self, choice):
        if choice=='default':choice=self.get()['defaults']['caller_id']
        number=self.m.settings.twilio_phone_number if choice=='twilio' else self.mobile()
        if not number:
            raise ValueError('The selected outbound caller ID is not configured')
        if choice=='mobile':
            try:
                verified=await self.m.gateway.verified_caller_id(number)
            except Exception:
                raise ValueError('Could not verify mobile caller ID with Twilio. No call was placed.') from None
            if not verified:
                raise ValueError('Verify the configured mobile in this Twilio account before using it as caller ID. No call was placed.')
        return {'caller_id':choice,'from_number':number}

    @staticmethod
    def fingerprint(request):
        return hashlib.sha256(request.model_dump_json(exclude={'request_id'}).encode()).hexdigest()

    def previous(self, request):
        if not request.request_id:return None
        with self.db.connect() as c:
            row=c.execute('SELECT call_id,request_hash FROM call_origination WHERE request_id=?',(str(request.request_id),)).fetchone()
        if not row:return None
        if row['request_hash']!=self.fingerprint(request):
            raise ValueError('This request ID belongs to a different call brief. Do not retry an uncertain call with a new ID.')
        return row['call_id']

    def for_call(self, call_id):
        with self.db.connect() as c:
            row=c.execute('SELECT caller_id,from_number,request_id FROM call_origination WHERE call_id=?',(call_id,)).fetchone()
        return dict(row) if row else None

    def find_request(self, request_id):
        with self.db.connect() as c:
            row=c.execute('SELECT call_id FROM call_origination WHERE request_id=?',(str(request_id),)).fetchone()
        if not row:raise KeyError('Submission not recorded yet. Check again; do not redial blindly.')
        return self.m.result(row['call_id'])
