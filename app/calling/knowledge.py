"""Small owner-curated directory. Every fact is approved for unverified callers."""
import re
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.calling.context import utcnow


class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=120)
    phone_number: str = Field(default='', pattern=r'^(?:|\+[1-9][0-9]{7,14})$')


class FactUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fact_id: UUID = Field(default_factory=uuid4)
    contact_id: UUID | None = None
    topic: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=600)
    source: str = Field(min_length=1, max_length=120)
    expires_at: AwareDatetime
    approved_for_callers: bool

    @field_validator('expires_at')
    @classmethod
    def expiry(cls, value):
        if not utcnow() < value <= utcnow() + timedelta(days=90):
            raise ValueError('Facts must expire within ninety days; refresh when verified')
        return value

    @field_validator('approved_for_callers')
    @classmethod
    def disclosure(cls, value):
        if not value:
            raise ValueError('Store only facts explicitly approved for unverified callers')
        return value

    @field_validator('text')
    @classmethod
    def short_fact(cls, value):
        if not value.strip() or len(value.encode('utf-8')) > 250:
            raise ValueError('Each fact must fit 250 UTF-8 bytes; split longer notes')
        return value


def words(text):
    return set(re.findall(r'[^\W_]{3,}', text.casefold(), flags=re.UNICODE))


class CallKnowledge:
    def __init__(self, db):
        self.db = db
        with db.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS concierge_contacts (
                    contact_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    phone_number TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS concierge_facts (
                    fact_id TEXT PRIMARY KEY, contact_id TEXT REFERENCES concierge_contacts(contact_id),
                    topic TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL,
                    expires_at TEXT NOT NULL, updated_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE INDEX IF NOT EXISTS concierge_facts_expiry ON concierge_facts(active,expires_at);
            ''')

    def matching_contacts(self, number):
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', number or ''):
            return []
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                'SELECT name FROM concierge_contacts WHERE phone_number=? ORDER BY name LIMIT 3', (number,))]

    def contact(self, body: ContactUpdate):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            count = conn.execute('SELECT count(*) FROM concierge_contacts').fetchone()[0]
            exists = conn.execute('SELECT 1 FROM concierge_contacts WHERE contact_id=?', (str(body.contact_id),)).fetchone()
            if count >= 200 and not exists:
                raise ValueError('At most 200 curated contacts')
            conn.execute('INSERT INTO concierge_contacts VALUES (?,?,?,?) ON CONFLICT(contact_id) DO UPDATE SET '
                'name=excluded.name,phone_number=excluded.phone_number,updated_at=excluded.updated_at',
                (str(body.contact_id), body.name.strip(), body.phone_number, utcnow().isoformat()))
        return {'contact_id': str(body.contact_id), 'status': 'saved'}

    def fact(self, body: FactUpdate):
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if body.contact_id and not conn.execute('SELECT 1 FROM concierge_contacts WHERE contact_id=?',
                                                     (str(body.contact_id),)).fetchone():
                raise ValueError('Contact not found')
            count = conn.execute('SELECT count(*) FROM concierge_facts').fetchone()[0]
            if count >= 500 and not conn.execute('SELECT 1 FROM concierge_facts WHERE fact_id=?', (str(body.fact_id),)).fetchone():
                raise ValueError('At most 500 facts; update existing IDs')
            conn.execute('INSERT INTO concierge_facts VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(fact_id) DO UPDATE SET '
                'contact_id=excluded.contact_id,topic=excluded.topic,text=excluded.text,source=excluded.source,'
                'expires_at=excluded.expires_at,updated_at=excluded.updated_at,active=1',
                (str(body.fact_id),str(body.contact_id) if body.contact_id else None,body.topic.strip(),body.text.strip(),
                 body.source.strip(),body.expires_at.isoformat(),utcnow().isoformat()))
        return {'fact_id':str(body.fact_id),'status':'active','expires_at':body.expires_at.isoformat()}

    def retire(self, fact_id):
        with self.db.connect() as conn:
            cursor=conn.execute('UPDATE concierge_facts SET active=0,updated_at=? WHERE fact_id=?', (utcnow().isoformat(),fact_id))
            if not cursor.rowcount:
                raise KeyError(fact_id)
        return {'fact_id':fact_id,'status':'retired'}

    def search(self, query='', *, limit=12, caller_number='', owner=False):
        """Literal bounded search, not SQL/model-generated queries. Numbers never go to Luna."""
        stamp=utcnow()
        with self.db.connect() as conn:
            rows=conn.execute('SELECT f.*,c.name AS contact_name,c.phone_number FROM concierge_facts f '
                'LEFT JOIN concierge_contacts c USING(contact_id) WHERE f.active=1 ORDER BY f.updated_at DESC LIMIT 500').fetchall()
            contacts=[dict(r) for r in conn.execute('SELECT * FROM concierge_contacts ORDER BY name LIMIT 200')] if owner else []
        terms=words(query)
        ranked=[]
        for row in rows:
            # Compare parsed dates: owner-supplied offsets need not be UTC.
            from datetime import datetime
            if datetime.fromisoformat(row['expires_at']) <= stamp:
                continue
            match=bool(caller_number and row['phone_number']==caller_number)
            name=row['contact_name'] or ''
            if not owner and row['contact_id'] and not (match or words(name) & terms):
                continue
            score=len(terms & words(row['topic']+' '+row['text']+' '+name)) + (3 if match else 0)
            if query and score==0:
                continue
            fact={k:row[k] for k in ('fact_id','contact_id','contact_name','topic','text','source','expires_at','updated_at')}
            ranked.append((score,fact))
        ranked.sort(key=lambda x:x[0],reverse=True)
        result={'facts':[f for _,f in ranked[:limit]],'disclosure':'All facts approved for unverified callers; contact match is not authentication.'}
        if owner:
            result['contacts']=contacts
        return result

    def still_current(self, facts):
        """Do not inject a fact retired, changed or expired while the model was working."""
        current={f['fact_id']:f for f in self.search(owner=True,limit=500)['facts']}
        return [f for f in facts if current.get(f['fact_id'])==f]
