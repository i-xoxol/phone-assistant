"""Audience-scoped, current owner statements. No inference from calendar titles."""
from dataclasses import dataclass
from datetime import datetime,timedelta
from uuid import uuid4

ALLOWED={'whereabouts':{'office','teaching','away','unavailable'},
         'busy_reason':{'meeting','teaching','on_a_call','busy'}}


class PrivateNotices:
    def __init__(self,store):self.store=store

    def set(self,audience_kind,audience_id,kind,value,effective_from,expires_at,*,source,now,notice_id=None):
        if source!='owner' or audience_kind not in ('contact','appointment') or not audience_id:
            raise ValueError('Explicit owner source and private audience required')
        if not effective_from<=now<expires_at<=now+timedelta(days=7):raise ValueError('Use a current bounded notice')
        if kind in ALLOWED:
            if audience_kind!='contact' or value not in ALLOWED[kind]:raise ValueError('Unsupported broad status')
        elif kind=='expected_return':
            if audience_kind!='contact' or not now<datetime.fromisoformat(value)<=expires_at:raise ValueError('Invalid expected return')
        elif kind=='delay_minutes':
            if audience_kind!='appointment' or not value.isdecimal() or not 0<int(value)<=120:raise ValueError('Invalid appointment delay')
        else:raise ValueError('Unsupported status category')
        nid=notice_id or str(uuid4())
        with self.store.connect() as db:
            old=db.execute('SELECT audience_kind,audience_id,kind FROM appointment_private_notices WHERE notice_id=?',(nid,)).fetchone()
            if old and tuple(old)!=(audience_kind,audience_id,kind):raise ValueError('Notice audience/category cannot be rebound')
            db.execute('UPDATE appointment_private_notices SET expires_at=?,revision=revision+1 WHERE audience_kind=? AND audience_id=? '
                'AND kind=? AND notice_id!=? AND expires_at>?',
                (now.isoformat(),audience_kind,audience_id,kind,nid,now.isoformat()))
            db.execute('INSERT INTO appointment_private_notices VALUES(?,1,?,?,?,?,?,?,?) '
                'ON CONFLICT(notice_id) DO UPDATE SET revision=revision+1,value=excluded.value,effective_from=excluded.effective_from,'
                'expires_at=excluded.expires_at,source=excluded.source',
                (nid,audience_kind,audience_id,kind,value,effective_from.isoformat(),expires_at.isoformat(),source))
        return nid

    def for_family(self,contacts,*,number,ambiguous,contradictory,now):
        if not number or ambiguous or contradictory or len(contacts)!=1:return {'status':'needs_owner'}
        contact=contacts[0]
        if not contact.get('contact_id') or contact.get('phone_number')!=number:return {'status':'needs_owner'}
        with self.store.connect() as db:
            relationship=db.execute('SELECT * FROM appointment_relationships WHERE contact_id=?',(contact['contact_id'],)).fetchone()
            if not relationship or not relationship['owner_established'] or relationship['relationship'] not in ('wife','mother','friend'):
                return {'status':'needs_owner'}
            notices=[dict(row) for row in db.execute('SELECT kind,value,revision FROM appointment_private_notices '
                "WHERE audience_kind='contact' AND audience_id=? AND source='owner' AND effective_from<=? AND expires_at>?",
                (contact['contact_id'],now.isoformat(),now.isoformat()))]
        return {'status':'current','notices':notices,'booking_authority_expanded':False}
