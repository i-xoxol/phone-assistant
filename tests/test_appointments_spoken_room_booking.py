"""A confirmed free slot stalled because E ten sixty-six was not E 1066."""
from datetime import timedelta

import pytest

from app.appointments import confirmation
from app.appointments.evidence import non_substantive_status
from app.appointments.registry import appointment
from app.calling.live_session import MediaBridge
from tests.test_calls import service
from tests.test_call_updates import UpdateConnection
from tests.test_appointments_gateway import prepare,speech
from tests.appointment_fixtures import NOW


LOCATION='In person, East Building, West Campus, room E 1066'


def office(service,tmp_path):
    client,m,p,g,cid,old=prepare(service,tmp_path)
    d=g.store.get('appointment_drafts','draft_id',old['draft_id'])
    result=client.portal.call(g.check,cid,dict(request_id=old['request_id'],first_name=d['first_name'],last_name=d['last_name'],
        purpose=d['purpose'],start=d['start'],end=d['end'],time_zone=d['zone'],arrangement=LOCATION))
    value=appointment(g.store.get('appointment_drafts','draft_id',result['draft_id']))
    return client,m,p,g,cid,result,value


@pytest.mark.parametrize('room',[
    'E ten sixty-six','E ten sixty six','E one zero six six','E one oh six six',
    'E one thousand sixty six','E 10 66','E-1066','E 1 0 6 6',
])
def test_exact_complete_readback_accepts_equivalent_room_codes_only(service,tmp_path,room):
    client,m,p,g,cid,result,value=office(service,tmp_path)
    text=result['readback'].replace('E 1066',room)
    assert confirmation.matches(text,value,short=False)
    assert not confirmation.matches(text.replace('10:00 AM','11:10 AM'),value,short=False)
    assert p.writes==0


@pytest.mark.parametrize('room',[
    'E ten sixty-five','E one sixty-six','E 166','E 10660','E 10 65','E 1 66',
    'E 1066 or E 1065','not E ten sixty-six','East Building, other campus, E 1066',
])
def test_room_readback_cannot_drop_a_zero_or_extra_location_content(service,tmp_path,room):
    client,m,p,g,cid,result,value=office(service,tmp_path)
    assert not confirmation.matches(result['readback'].replace('E 1066',room),value,short=False)


@pytest.mark.parametrize('change',[
    ('10:00 AM','11:10 AM'),('10:00 AM','10:00 PM'),('October 26th','October 27th'),
    ('15 minutes','one hour and fifteen minutes'),('15 minutes','one hundred fifteen minutes'),
    ('10:00 AM','10:00 AM or 11:00 AM'),
])
def test_spoken_room_normalization_cannot_relax_date_time_or_duration(service,tmp_path,change):
    client,m,p,g,cid,result,value=office(service,tmp_path)
    text=result['readback'].replace('E 1066','E ten sixty-six').replace(*change)
    assert not confirmation.matches(text,value,short=False)


@pytest.mark.parametrize('mode',['accepted','expired_check','wrong_time','wrong_room','refusal','conditional','pending_audio','hangup'])
def test_native_controller_books_once_after_natural_agreement_and_catchup_message(service,tmp_path,mode):
    client,m,p,g,cid,result,value=office(service,tmp_path)
    m.settings.appointments_enabled=True;g.voice_workflow.bind(cid,result);g.voice_workflow.settle_seconds=.001
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    text=result['readback'].replace('E 1066','E ten sixty-six')
    if mode=='wrong_time':text=text.replace('10:00 AM','11:10 AM')
    if mode=='wrong_room':text=text.replace('sixty-six','sixty-five')
    speech(m,cid,'assistant',text,1000)
    speech(m,cid,'callee','No, do not book it.' if mode=='refusal' else 'Yeah, that works, but at eleven.' if mode=='conditional' else 'Yeah, that works.',2000)
    speech(m,cid,'assistant','Okay, great. Thanks. Just a moment while the server catches up.',3000)
    if mode=='pending_audio':m.caller_activity[cid]={'last_speech_ms':5000}
    if mode=='hangup':m.transition(cid,'completed')
    with g.store.connect() as db:old_checks={r['check_id']:r['expires_at'] for r in db.execute('SELECT * FROM appointment_checks WHERE draft_id=?',(result['draft_id'],))}
    if mode=='expired_check':g.clock=g.service.clock=p.clock=lambda:NOW+timedelta(seconds=35)
    async def run():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_ROOM_BOOKING');m.attach_bridge(cid,bridge)
        g.voice_workflow.notify(bridge);await g.voice_workflow.tasks[cid]
        with g.store.connect() as db:ops=[dict(r) for r in db.execute('SELECT * FROM booking_operations WHERE call_id=?',(cid,))]
        if mode in ('accepted','expired_check'):
            assert p.writes==1 and len(ops)==1 and ops[0]['state']=='confirmed'
            if mode=='expired_check':
                with g.store.connect() as db:
                    checks=[dict(r) for r in db.execute('SELECT * FROM appointment_checks WHERE draft_id=?',(result['draft_id'],))]
                    assert len(checks)==len(old_checks)+1
                    assert all(r['expires_at']==old_checks[r['check_id']] for r in checks if r['check_id'] in old_checks)
                    assert [r['checked_at'] for r in checks if r['check_id'] not in old_checks]==[(NOW+timedelta(seconds=35)).isoformat()]
            g.voice_workflow.notify(bridge);await g.voice_workflow.tasks[cid]
            assert p.writes==1
        else:assert p.writes==0 and not ops
    client.portal.call(run)


@pytest.mark.parametrize('text',[
    'Thanks, just a moment while the server catches up at 11 AM.',
    'Okay, the server catches up, but room E 1065 instead.',
    'The server catches up; do not book it.',
])
def test_catchup_status_does_not_hide_changed_quantities_location_or_refusal(text):
    assert not non_substantive_status(text)
