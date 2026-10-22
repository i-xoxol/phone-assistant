"""Booking consent and disclosure must follow complete, current spoken evidence."""
from dataclasses import replace
from datetime import timedelta
import pytest
from app.appointments.gateway import complete_readback,readback_text
from app.appointments.registry import appointment
from tests.appointment_fixtures import setup
from tests.test_calls import service
from tests.test_appointments_gateway import prepare,speech,agree


@pytest.mark.parametrize('old,new',[
    ('October 26','October 27'),('10:00 AM','11:10 AM'),('15 minutes','115 minutes'),
    ('Office A','Office AB'),('Casey Example','Casey Exampleton'),
    ('2026','2027'),('10:00 AM','10:00 PM')])
def test_readback_requires_exact_fields(tmp_path,old,new):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    text='Casey Example, '+readback_text(value)
    assert complete_readback(text,value)
    assert not complete_readback(text.replace(old,new),value)


@pytest.mark.parametrize('extra',[
    ' Actually October 27 2026.', ' Or 11:10 AM.', ' Not Office A.', ' Actually 30 minutes.'])
def test_contradictory_readback_does_not_authorize(tmp_path,extra):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    assert not complete_readback('Casey Example, '+readback_text(value)+extra,value)


@pytest.mark.parametrize('old,new',[
    ('10:00 AM','eleven ten a.m.'),('10:00 AM','11 10 AM'),('10:00 AM','eleven-ten a.m.'),
    ('15 minutes','one hundred fifteen minutes'),('15 minutes','one hour and fifteen minutes'),
    ('15 minutes','one hundred and fifteen minutes'),('15 minutes','1 hour and 15 minutes')])
def test_complete_temporal_expression_cannot_match_a_suffix(tmp_path,old,new):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    assert not complete_readback(('Casey Example, '+readback_text(value)).replace(old,new),value)


@pytest.mark.parametrize('field,value',[('last_name','May'),('purpose','May course guidance'),('location','May Hall')])
def test_exact_non_temporal_fields_can_contain_month_words(tmp_path,field,value):
    s,db,p,d,state,now=setup(tmp_path);value=replace(appointment(d),**{field:value})
    assert complete_readback(value.first_name+' '+value.last_name+', '+readback_text(value),value)


@pytest.mark.parametrize('spoken',['10 AM','10 a.m.','ten a.m.'])
def test_exact_spoken_time_and_duration_variants(tmp_path,spoken):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    text=('Casey Example, '+readback_text(value)).replace('10:00 AM',spoken).replace('15 minutes','fifteen minutes')
    assert complete_readback(text,value)


def test_unspoken_seconds_and_wrong_dst_zone_cannot_authorize(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);value=appointment(d)
    shifted=replace(value,interval=replace(value.interval,start=value.interval.start+timedelta(seconds=1),end=value.interval.end+timedelta(seconds=1)))
    assert not complete_readback('Casey Example, '+readback_text(shifted),shifted)
    assert not complete_readback(('Casey Example, '+readback_text(value)).replace('America/New_York','Eastern Standard Time'),value)


@pytest.mark.parametrize('replacement',['October 27 2026 at 10:00 AM','October 26 2026 at 11:10 AM'])
def test_wrong_spoken_date_or_time_plus_yes_cannot_write(service,tmp_path,replacement):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    text='Casey Example, '+readback_text(value)
    speech(m,cid,'assistant',text.replace('October 26 2026 at 10:00 AM',replacement),1000)
    speech(m,cid,'callee','Yes',2000)
    with pytest.raises(ValueError):client.portal.call(gateway.book,cid,{
        'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    assert p.writes==0


def test_correct_then_wrong_readback_before_yes_cannot_write(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    speech(m,cid,'assistant','Casey Example, '+readback_text(value),1000)
    speech(m,cid,'assistant','Actually October 27 2026 at 11:10 AM.',1500)
    speech(m,cid,'callee','Yes',2500)
    with pytest.raises(ValueError):client.portal.call(gateway.book,cid,{
        'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    assert p.writes==0


def test_fragmented_correct_readback_still_books_once(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    with m.db.connect() as db:db.execute("UPDATE call_updates SET status='acknowledged' WHERE call_id=?",(cid,))
    text='Casey Example, '+readback_text(value)
    speech(m,cid,'assistant',text[:28],1000)
    speech(m,cid,'assistant',text[28:],1500)
    speech(m,cid,'callee','Yes',2500)
    args={'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'}
    assert client.portal.call(gateway.book,cid,args)['state']=='confirmed'
    assert client.portal.call(gateway.book,cid,args)['state']=='confirmed' and p.writes==1


def test_yes_interrupting_incomplete_readback_cannot_write(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    agree(m,gateway,cid,r)
    with m.db.connect() as db:db.execute("UPDATE transcript_events SET start_ms=1100,end_ms=1200 WHERE call_id=? AND speaker='callee'",(cid,))
    with pytest.raises(ValueError):client.portal.call(gateway.book,cid,{
        'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    assert p.writes==0


@pytest.mark.parametrize('turns,allowed',[
    ([('callee','My name is Casey Example.')],True),
    ([('callee','My name is Casey Exampleton.')],False),
    ([('callee','My name is Casey Example.'),('callee','Actually, my name is Jordan Else.')],False),
    ([('callee','My name is Casey Example.'),('callee','Actually, my surname is Else.')],False),
    ([('callee','My name is Casey Example.'),('callee',"That is not my name.")],False),
    ([('callee','My name is Casey Example.'),('callee','Actually, Jordan Else.')],False),
    ([('callee','My name is Casey Example.'),('callee',"My name's Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('callee',"My last name's Else.")],False),
    ([('callee','My name is Casey Example.'),('callee','Call me Jordan Else.')],False),
    ([('callee','My name is Casey Example.'),('callee','My name is')],False),
    ([('callee','My name is Casey Example.'),('callee','My last name is')],False),
    ([('callee','My name is Casey Example.'),('callee',"That's wrong. My name is Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('callee',"Thanks. My name is Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('assistant','How can I help?'),('callee',"I'm calling about my appointment.")],True),
    ([('callee','My name is Casey Example.'),('assistant','How can I help?'),('callee',"It's about my appointment.")],True),
    ([('callee','My name is Casey Example.'),('callee',"I'm calling about my appointment; my name is Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('callee',"I'm calling about my appointment; I am Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('callee',"I'm calling about my appointment as Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('callee',"I'm calling myself Jordan Else.")],False),
    ([('callee','My name is Casey Example.'),('callee',"I'm calling about the appointment, not Casey Example.")],False),
    ([('callee','My name is Casey Example.'),('assistant','Your last-name?'),('callee','Else')],False),
    ([('callee','My name is Casey Example.'),('callee','My lastname is Else.')],False),
    ([('callee','My name is Casey'),('callee',' Example')],True),
    ([('assistant','Your full name?'),('callee','Casey'),('callee',' Example')],True),
    ([('assistant','Your first name?'),('callee','Casey'),('assistant','And your surname?'),('callee','Example')],True),
    ([('assistant','Your first name?'),('callee','Casey')],False),
    ([('assistant','Your surname?'),('callee','Example')],False),
    ([('assistant','Your first name?'),('callee','Casey'),('assistant','Your surname?'),('callee','Exampleton')],False),
    ([('callee','Please tell me about Casey Example and that appointment.')],False),
    ([('callee','I am calling for Casey Example.')],False),
    ([('callee','My name is Casey Example.'),('callee','Actually, my name is Casey.')],False),
    ([('callee','My name is Casey Example.'),('assistant','Your full name?'),('callee','Casey')],False),
    ([('assistant','Your first name?'),('callee','Casey'),('assistant','Your surname?'),('callee','Example'),
        ('assistant','Your first name?'),('callee','Jordan')],False),
])
def test_only_complete_current_identity_reaches_private_lookup(service,tmp_path,monkeypatch,turns,allowed):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);looked=[]
    async def lookup(first,last,*,contradictory=False):
        looked.append(contradictory)
        return {'status':'needs_owner'} if contradictory else {'status':'not_found'}
    monkeypatch.setattr(gateway.service,'own_appointment',lookup)
    for i,(speaker,text) in enumerate(turns):speech(m,cid,speaker,text,1000+i*500)
    result=client.portal.call(gateway.own,cid,{'first_name':'Casey','last_name':'Example'})
    assert looked==[not allowed] and result=={'status':'not_found' if allowed else 'needs_owner'}


def test_name_change_during_provider_read_suppresses_details(service,tmp_path,monkeypatch):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    speech(m,cid,'callee','My name is Casey Example.',1000)
    async def lookup(*args,**kwargs):
        speech(m,cid,'callee','Actually, my name is Jordan Else.',2000)
        return {'status':'match','location':'PRIVATE'}
    monkeypatch.setattr(gateway.service,'own_appointment',lookup)
    assert client.portal.call(gateway.own,cid,{'first_name':'Casey','last_name':'Example'})=={'status':'needs_owner'}


def test_uncaptioned_name_change_suppresses_details(service,tmp_path,monkeypatch):
    client,m,p,gateway,cid,r=prepare(service,tmp_path)
    speech(m,cid,'callee','My name is Casey Example.',1000)
    m.caller_activity[cid]={'epoch':2,'last_speech_ms':3000}
    async def lookup(first,last,*,contradictory=False):
        return {'status':'needs_owner'} if contradictory else {'status':'match','location':'PRIVATE'}
    monkeypatch.setattr(gateway.service,'own_appointment',lookup)
    assert client.portal.call(gateway.own,cid,{'first_name':'Casey','last_name':'Example'})=={'status':'needs_owner'}


def test_separate_names_disclose_only_the_verified_appointment_then_correction_blocks(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    assert client.portal.call(gateway.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],
        'idempotency_key':'one'})['state']=='confirmed'
    for i,(speaker,text) in enumerate([('assistant','Your first name?'),('callee','Casey'),
            ('assistant','Your surname?'),('callee','Example')]):speech(m,cid,speaker,text,3000+i*500)
    result=client.portal.call(gateway.own,cid,{'first_name':'Casey','last_name':'Example'})
    assert result['status']=='match' and result['location']=='Office A'
    speech(m,cid,'callee','Actually, my name is Casey Exampleton.',6000)
    assert client.portal.call(gateway.own,cid,{'first_name':'Casey','last_name':'Example'})=={'status':'needs_owner'}
    assert p.writes==1


def test_confirmation_marker_and_details_in_separate_deltas_are_observed(service,tmp_path):
    client,m,p,gateway,cid,r=prepare(service,tmp_path);agree(m,gateway,cid,r)
    op=client.portal.call(gateway.book,cid,{'draft_id':r['draft_id'],'check_id':r['check_id'],'idempotency_key':'one'})
    value=appointment(gateway.store.get('appointment_drafts','draft_id',r['draft_id']))
    speech(m,cid,'assistant','Confirmed.',3000)
    speech(m,cid,'assistant',readback_text(value),4000)
    speech(m,cid,'callee','Thank you',5000)
    result=gateway.delivery_status(cid,op['operation_id'])
    assert result['spoken_observed'] and result['caller_receipt_observed'] and p.writes==1
