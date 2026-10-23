import asyncio
from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace
import pytest
from tests.test_calls import service
from tests.appointment_fixtures import NOW,SLOT,FakeProvider
from app.appointments.gateway import AppointmentGateway,readback_text
from app.appointments.schemas import SlotRequest
from app.appointments.registry import appointment
from app.calling.style import STYLE_POLICY
from app.calling.inbound import incoming_session_config
from app.calling.prompts import voice_prompt

def prepare(service,tmp_path):
    client,m,g=service;m.settings.appointment_database_path=tmp_path/'private.sqlite3'
    m.settings.appointments_writes_enabled=True
    p=FakeProvider(lambda:NOW);gateway=AppointmentGateway(m,p,clock=lambda:NOW);m.appointments=gateway
    # Offline provider fixtures also isolate the external name model. Resolver
    # integration tests inject their own response explicitly.
    gateway.name_resolver=None
    cid=m.db.create_inbound('CA_fixture','+12025550111',15,10,'concierge',True)
    m.transition(cid,'in_progress')
    with m.db.connect() as db:db.execute('UPDATE calls SET created_at=?,started_at=? WHERE call_id=?',(NOW.isoformat(),NOW.isoformat(),cid))
    args=SlotRequest(first_name='Casey',last_name='Example',purpose='course guidance',start=SLOT.start,end=SLOT.end,
        time_zone=SLOT.zone,arrangement='Office A')
    result=client.portal.call(gateway.check,cid,args.model_dump(mode='json'))
    return client,m,p,gateway,cid,result

def speech(m,cid,speaker,text,start,event=None):
    m.db.transcript(cid,{'type':'session.input_transcript.delta' if speaker=='callee' else 'session.output_transcript.delta',
        'event_id':event or str(uuid4()),'start_ms':start,'end_ms':start+500,'delta':text})

def agree(m,gateway,cid,result,*,ack=True,full=True):
    if ack:
        with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    value=appointment(gateway.store.get('appointment_drafts','draft_id',result['draft_id']))
    speech(m,cid,'assistant','Casey Example, '+(readback_text(value) if full else 'Is that okay?'),1000)
    speech(m,cid,'callee','Yes',2000)

def test_real_caller_evidence_and_private_result(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    result=client.portal.call(gateway.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    assert result['state']=='confirmed' and p.writes==1
    assert result['confirmed_details']['duration_seconds']==900
    profile=m.incoming_context(m.db.get(cid))
    assert 'Casey' not in str(profile) and 'Office A' not in str(profile)
    assert gateway.delivery_status(cid,result['operation_id'])['spoken_observed'] is False

@pytest.mark.parametrize('missing',['ack','readback','yes','new_audio','withdrawal'])
def test_model_cannot_claim_consent_without_evidence(service,tmp_path,missing):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    agree(m,gateway,cid,r,ack=missing!='ack',full=missing!='readback')
    if missing=='yes':speech(m,cid,'callee','No',3000)
    if missing=='withdrawal':speech(m,cid,'callee','Actually Friday instead',3000)
    if missing=='new_audio':m.caller_activity[cid]={'epoch':2,'last_speech_ms':3000}
    with pytest.raises(ValueError):client.portal.call(gateway.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    assert p.writes==0

def test_readback_ack_is_not_spoken_receipt(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    op=client.portal.call(gateway.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    result=client.portal.call(gateway.status,cid,op['operation_id'])
    assert result['state']=='confirmed' and result['delivery']['injection_accepted']
    assert not result['delivery']['spoken_observed'] and not result['delivery']['caller_receipt_observed']
    assert p.writes==1

def test_deadline_uses_configured_value_not_hardcoded(service,tmp_path):
    client,m,g=service;m.settings.supervisor_wait_seconds=90
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    with gateway.store.connect() as db:deadline=db.execute('SELECT deadline FROM appointment_requests').fetchone()[0]
    assert deadline==(NOW+timedelta(seconds=90)).isoformat()

def test_shared_style_keeps_voice_and_required_endings(service):
    client,m,g=service;m.settings.openai_voice='meridian'
    record={'direction':'inbound','inbound_mode':'concierge','phone_number':'+12025550111','transcript':[],
        'inbound_booking_allowed':True}
    incoming=incoming_session_config(m.settings,record)
    outgoing=voice_prompt(m.settings,{'recipient':'Fixture','objective':'Ask a question','context':'','constraints':[]})
    assert STYLE_POLICY in incoming['instructions'] and STYLE_POLICY in outgoing
    assert incoming['audio']['output']['voice']=='meridian'
    assert 'Thank you. Goodbye.' in incoming['instructions'] and 'Thank you. Goodbye.' in outgoing

def test_feature_default_has_no_calendar_tools_or_grants(service):
    client,m,g=service
    assert m.appointments is None and not m.settings.appointments_enabled
    incoming=incoming_session_config(m.settings,{'inbound_mode':'concierge','phone_number':'','direction':'inbound'})
    assert 'book_appointment' not in [t['name'] for t in incoming['delegation']['responses']['tools']]

def test_new_mcp_scope_is_required():
    from app.appointments.mcp import require
    with pytest.raises(PermissionError):require('phone.appointments.book')

def test_existing_appointment_name_must_be_self_identification_not_third_party_mention(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    speech(m,cid,'callee','Please tell me about Casey Example and that appointment.',1000)
    assert client.portal.call(gateway.own,cid,{'first_name':'Casey','last_name':'Example'})=={'status':'needs_owner'}

def test_curated_friend_gets_evening_freebusy_without_titles_or_booking_expansion(service,tmp_path):
    from dataclasses import replace
    from app.calling.knowledge import ContactUpdate
    from app.appointments.contracts import Coverage,CalendarEvent,EventIdentity,Interval
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    contact=m.knowledge.contact(ContactUpdate(name='Casey Example',phone_number='+12025550111'))
    with gateway.store.connect() as db:db.execute('INSERT INTO appointment_relationships VALUES(?,?,1)',(contact['contact_id'],'friend'))
    p.capabilities=replace(p.capabilities,owner_agenda=True)
    start=NOW+timedelta(hours=11);end=start+timedelta(hours=1)
    async def agenda(interval):return Coverage(interval,NOW,NOW+timedelta(seconds=30),True,(
        CalendarEvent(EventIdentity(p.identity,'private-event'),'r',Interval(start,end,SLOT.zone),owner_label='PRIVATE EVENT TITLE'),))
    p.read_agenda=agenda
    args={'start':start.isoformat(),'end':end.isoformat(),'time_zone':SLOT.zone}
    result=client.portal.call(gateway.family_availability,cid,args)
    assert result['status']=='current' and not result['booking_authority_expanded']
    assert 'PRIVATE EVENT TITLE' not in str(result) and 'label' not in str(result)
    speech(m,cid,'callee','My name is Different Person',1000)
    assert client.portal.call(gateway.family_availability,cid,args)=={'status':'needs_owner'}
