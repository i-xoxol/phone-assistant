"""Small owner-curated directory. Every fact is approved for unverified callers."""
import re
from typing import Literal
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.calling.context import utcnow


class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    contact_id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=120)
    phone_number: str = Field(default='', pattern=r'^(?:|\+[1-9][0-9]{7,14})$')
    preferred_language: str | None = Field(default=None,pattern=r'^(?:|[a-z]{2,3}(?:-[A-Z]{2})?)$',max_length=12)
    preferred_name: str | None = Field(default=None,max_length=120)
    conversation_style: Literal['familiar','formal'] | None = None


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
                CREATE TABLE IF NOT EXISTS concierge_contact_preferences (
                    contact_id TEXT PRIMARY KEY REFERENCES concierge_contacts(contact_id),
                    preferred_language TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS concierge_contact_styles (
                    contact_id TEXT PRIMARY KEY REFERENCES concierge_contacts(contact_id),
                    preferred_name TEXT NOT NULL DEFAULT '',
                    conversation_style TEXT NOT NULL DEFAULT 'formal'
                        CHECK(conversation_style IN ('familiar','formal')));
                CREATE TABLE IF NOT EXISTS caller_languages (
                    phone_number TEXT PRIMARY KEY, language TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(source IN ('caller','owner')), updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS call_languages (
                    call_id TEXT PRIMARY KEY REFERENCES calls(call_id), language TEXT NOT NULL, updated_at TEXT NOT NULL);
            ''')

    def matching_contacts(self, number):
        if not re.fullmatch(r'\+[1-9][0-9]{7,14}', number or ''):
            return []
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(
                '''SELECT c.name,COALESCE(p.preferred_language,'') AS preferred_language,
                COALESCE(s.preferred_name,'') AS preferred_name,COALESCE(s.conversation_style,'formal') AS conversation_style
                FROM concierge_contacts c LEFT JOIN concierge_contact_preferences p USING(contact_id)
                LEFT JOIN concierge_contact_styles s USING(contact_id)
                WHERE c.phone_number=? ORDER BY c.name LIMIT 3''', (number,))]

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
            if body.preferred_language is not None:
                conn.execute('INSERT INTO concierge_contact_preferences VALUES (?,?) ON CONFLICT(contact_id) '
                    'DO UPDATE SET preferred_language=excluded.preferred_language',
                    (str(body.contact_id),body.preferred_language))
                if body.phone_number:
                    self._set_language(conn,body.phone_number,body.preferred_language or 'en','owner',sync_contacts=False)
            if body.preferred_name is not None or body.conversation_style is not None:
                conn.execute('INSERT OR IGNORE INTO concierge_contact_styles(contact_id) VALUES (?)',(str(body.contact_id),))
                if body.preferred_name is not None:
                    conn.execute('UPDATE concierge_contact_styles SET preferred_name=? WHERE contact_id=?',
                        (body.preferred_name.strip(),str(body.contact_id)))
                if body.conversation_style is not None:
                    conn.execute('UPDATE concierge_contact_styles SET conversation_style=? WHERE contact_id=?',
                        (body.conversation_style,str(body.contact_id)))
        return {'contact_id': str(body.contact_id), 'status': 'saved'}

    @staticmethod
    def _set_language(conn, number, language, source, *, sync_contacts=True):
        if (not conn.execute('SELECT 1 FROM caller_languages WHERE phone_number=?',(number,)).fetchone()
            and conn.execute('SELECT count(*) FROM caller_languages').fetchone()[0]>=1000):
            raise ValueError('Caller preference directory is full')
        conn.execute('INSERT INTO caller_languages VALUES (?,?,?,?) ON CONFLICT(phone_number) DO UPDATE SET '
                     'language=excluded.language,source=excluded.source,updated_at=excluded.updated_at',
                     (number,language,source,utcnow().isoformat()))
        # Update only language, never identity, names, style, or disclosure permissions.
        matches=list(conn.execute('SELECT contact_id FROM concierge_contacts WHERE phone_number=?',(number,)))
        if sync_contacts and len(matches)==1:
            conn.execute('INSERT INTO concierge_contact_preferences VALUES (?,?) ON CONFLICT(contact_id) '
                         'DO UPDATE SET preferred_language=excluded.preferred_language',(matches[0]['contact_id'],language))

    def set_language(self, number, language, *, source='owner', call_id=None):
        from app.calling.languages import LanguageCode
        LanguageCode(language=language)
        if number and not re.fullmatch(r'\+[1-9][0-9]{7,14}',number):
            raise ValueError('Valid presented phone number required')
        with self.db.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if number:
                self._set_language(conn,number,language,source)
            if call_id:
                conn.execute('INSERT INTO call_languages VALUES (?,?,?) ON CONFLICT(call_id) DO UPDATE SET '
                             'language=excluded.language,updated_at=excluded.updated_at',(call_id,language,utcnow().isoformat()))
        return {'language':language,'remembered':bool(number),'status':'saved' if number else 'this_call_only'}

    def language_for(self, number, call_id=None):
        with self.db.connect() as conn:
            saved=conn.execute('SELECT language,source,updated_at FROM caller_languages WHERE phone_number=?',(number,)).fetchone()
            current=conn.execute('SELECT language,updated_at FROM call_languages WHERE call_id=?',(call_id,)).fetchone()
        if current and (not saved or current['updated_at']>saved['updated_at']):
            return {'language':current['language'],'source':'current_call'}
        if saved:
            return {'language':saved['language'],'source':saved['source']}
        contacts=self.matching_contacts(number)
        language=contacts[0]['preferred_language'] if len(contacts)==1 else ''
        return {'language':language or 'en','source':'contact' if language else 'default'}

    def caller_book(self):
        with self.db.connect() as conn:
            rows=[dict(row) for row in conn.execute('''SELECT c.name,c.contact_id,c.phone_number,
                COALESCE(l.language,NULLIF(p.preferred_language,''),'en') AS language,
                COALESCE(l.source,'owner') AS language_source
                FROM concierge_contacts c LEFT JOIN concierge_contact_preferences p USING(contact_id)
                LEFT JOIN caller_languages l USING(phone_number)
                UNION ALL SELECT '',NULL,l.phone_number,l.language,l.source FROM caller_languages l
                WHERE NOT EXISTS (SELECT 1 FROM concierge_contacts c WHERE c.phone_number=l.phone_number)
                ORDER BY name,phone_number LIMIT 200''')]
        return rows

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
            contacts=[dict(r) for r in conn.execute('''SELECT c.*,COALESCE(p.preferred_language,'') AS preferred_language,
                COALESCE(s.preferred_name,'') AS preferred_name,COALESCE(s.conversation_style,'formal') AS conversation_style
                FROM concierge_contacts c LEFT JOIN concierge_contact_preferences p USING(contact_id)
                LEFT JOIN concierge_contact_styles s USING(contact_id)
                ORDER BY c.name LIMIT 200''')] if owner else []
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
            result['caller_preferences']=self.caller_book()
        return result

    def still_current(self, facts):
        """Do not inject a fact retired, changed or expired while the model was working."""
        current={f['fact_id']:f for f in self.search(owner=True,limit=500)['facts']}
        return [f for f in facts if current.get(f['fact_id'])==f]
