"""Preserve carrier forwarding metadata without replacing presented caller ID."""
import re
from app.storage.models import E164


class IncomingRouting:
    def __init__(self,manager):
        self.m=manager
        with manager.db.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS incoming_routing (
                call_id TEXT PRIMARY KEY REFERENCES calls(call_id),forwarded_from TEXT NOT NULL,
                forwarding_reported INTEGER NOT NULL CHECK(forwarding_reported IN (0,1)))''')

    def save(self,cid,form):
        raw=str(form.get('ForwardedFrom','')).strip()
        number=raw if re.fullmatch(E164,raw) else ''
        with self.m.db.connect() as db:
            db.execute('INSERT OR IGNORE INTO incoming_routing VALUES (?,?,?)',(cid,number,int(bool(raw))))

    def get(self,record,*,for_voice=False):
        with self.m.db.connect() as db:
            row=db.execute('SELECT * FROM incoming_routing WHERE call_id=?',(record['call_id'],)).fetchone()
        reported=bool(row and row['forwarding_reported'])
        number=row['forwarded_from'] if row else ''
        ambiguous=bool(reported and number and number==record['phone_number'])
        result={'detected':True if reported else None,'source':'twilio_forwarded_from' if reported else 'not_supplied',
            'caller_identity_ambiguous':ambiguous,'forwarded_from_available':bool(number),
            'missed_call_context':self.m.settings.inbound_missed_call_context and record.get('direction')=='inbound',
            'note':'Missing metadata does not prove a direct call. Carrier forwarding may preserve, replace or withhold caller ID. Declined versus unanswered is not known.'}
        # Forwarding metadata may contain the owner's private mobile.
        if not for_voice: result['forwarded_from']=number
        return result
