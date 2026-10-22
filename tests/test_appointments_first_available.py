"""Earliest verified proposals still need exact current consent before a write."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from tests.appointment_fixtures import NOW,SLOT,setup
from tests.test_calls import service
from tests.test_appointments_gateway import prepare,speech,agree
from app.appointments.contracts import CalendarEvent,Coverage,EventIdentity,Interval,ProviderFault
from app.appointments.schemas import VOICE_TOOLS,VOICE_POLICY


def test_first_available_has_a_tool_without_mandatory_date_or_time():
    tools={t['name']:t for t in VOICE_TOOLS}
    schema=tools['find_first_available_slot']['parameters']
    assert 'start' not in schema.get('required',[]) and 'end' not in schema.get('required',[])
    assert 'duration_minutes' in schema['required']
    assert 'do not ask for a specific date or time' in VOICE_POLICY.lower()


def test_first_available_skips_occupied_minutes_and_does_not_write(tmp_path):
    svc,store,p,draft,state,now=setup(tmp_path)
    p.events['busy']=CalendarEvent(EventIdentity(p.identity,'busy'),'r',
        Interval(SLOT.start,SLOT.start+timedelta(minutes=17,seconds=20),SLOT.zone))
    found=asyncio.run(svc.find_slot('call-1','request-1',15))
    assert found['interval'].start==SLOT.start+timedelta(minutes=18)
    assert found['interval'].end==SLOT.start+timedelta(minutes=33)
    assert p.writes==0


@pytest.mark.parametrize('kind,reason',[
    ('partial','incomplete_coverage'),('future','future_coverage'),('expired','expired_coverage'),
    ('short_window','insufficient_interval_coverage'),('unknown_event','unknown_event_state'),
    ('provider','unavailable'),('unsupported','unsupported'),('timeout','deadline_budget')])
def test_cannot_claim_earliest_without_complete_fresh_coverage(tmp_path,kind,reason):
    svc,store,p,d,state,now=setup(tmp_path)
    async def coverage(interval):
        now[0]+=timedelta(milliseconds=100)  # Actual reads advance the clock.
        if kind=='provider':raise ProviderFault('unavailable')
        if kind=='timeout':raise TimeoutError()
        c=Coverage(interval,now[0],now[0]+timedelta(seconds=30),True,(p.office,))
        if kind=='partial':c=replace(c,complete=False,reason='known_conflict')
        if kind=='future':c=replace(c,checked_at=now[0]+timedelta(seconds=1))
        if kind=='expired':c=replace(c,valid_until=now[0])
        if kind=='short_window':c=replace(c,interval=SLOT)
        if kind=='unknown_event':c=replace(c,events=(p.office,CalendarEvent(EventIdentity(p.identity,'unknown'),'r',None,state='unknown')))
        return c
    p.read_availability=coverage
    if kind=='unsupported':p.capabilities=replace(p.capabilities,availability_search=False)
    result=asyncio.run(svc.find_slot('call-1','request-1',15))
    assert result['decision']=='unknown' and result['reason']==reason
    assert 'interval' not in result and p.writes==0


@pytest.mark.parametrize('change',['hangup','deadline','permission','correction'])
def test_search_revalidates_call_and_caller_after_io(tmp_path,change):
    svc,store,p,d,state,now=setup(tmp_path)
    def changed():
        if change=='hangup':state['active']=False
        if change=='deadline':now[0]+=timedelta(seconds=91)
        if change=='permission':state['allowed']=False
        if change=='correction':state['revision']='new-preference'
    p.read_hook=changed
    with pytest.raises(ValueError):asyncio.run(svc.find_slot('call-1','request-1',15))
    assert p.writes==0


def test_constraints_buffers_and_exact_boundary_are_respected(tmp_path):
    svc,store,p,d,state,now=setup(tmp_path)
    svc.policy=replace(svc.policy,buffer_before=5,buffer_after=5)
    p.events['busy']=CalendarEvent(EventIdentity(p.identity,'busy'),'r',Interval(SLOT.start,SLOT.end,SLOT.zone))
    result=asyncio.run(svc.find_slot('call-1','request-1',15,not_before=SLOT.start,not_after=SLOT.start+timedelta(hours=1)))
    assert result['interval'].start==SLOT.end+timedelta(minutes=5)
    p.events={}
    result=asyncio.run(svc.find_slot('call-1','request-1',15,not_before=SLOT.start,not_after=SLOT.end))
    assert result['interval']==SLOT
    assert result['reservation'] is False and p.writes==0


@pytest.mark.parametrize('guard',['missing_occurrence','cancelled_occurrence','outside_hours','closed_date','busy'])
def test_search_preserves_office_policy_and_only_reports_its_bounded_window(tmp_path,guard):
    svc,store,p,d,state,now=setup(tmp_path)
    if guard=='missing_occurrence':p.office=replace(p.office,kind='commitment',state='free')
    if guard=='cancelled_occurrence':p.office=replace(p.office,state='cancelled')
    if guard=='outside_hours':p.office=replace(p.office,interval=Interval(NOW,NOW+timedelta(minutes=30),SLOT.zone))
    if guard=='closed_date':svc.policy=replace(svc.policy,closed_dates=frozenset([SLOT.start.date()]))
    if guard=='busy':p.events['all-day']=CalendarEvent(EventIdentity(p.identity,'busy'),'r',p.office.interval)
    result=asyncio.run(svc.find_slot('call-1','request-1',15))
    assert result['decision']=='no_available_slot' and 'interval' not in result
    assert result['reason']=='no_verified_slot_in_search_window'
    assert result['search_end']==(NOW+timedelta(days=14)).isoformat() and p.writes==0


@pytest.mark.parametrize('duration,bounds',[(0,{}),(121,{}),(True,{}),(15,{'not_before':NOW+timedelta(days=15)}),
    (15,{'not_after':NOW+timedelta(days=15)}),(15,{'not_after':NOW-timedelta(minutes=1)}),
    (15,{'not_before':NOW.replace(tzinfo=None)})])
def test_search_inputs_are_bounded_before_provider_io(tmp_path,duration,bounds):
    svc,store,p,d,state,now=setup(tmp_path)
    def forbidden():raise AssertionError('invalid search must not query calendar')
    p.read_hook=forbidden
    with pytest.raises(ValueError):asyncio.run(svc.find_slot('call-1','request-1',duration,**bounds))


def first_call(service,tmp_path):
    from app.appointments.gateway import AppointmentGateway
    from tests.appointment_fixtures import FakeProvider
    client,m,_=service;m.settings.appointment_database_path=tmp_path/'private.sqlite3'
    m.settings.appointments_enabled=True;m.settings.appointments_writes_enabled=True
    p=FakeProvider(lambda:NOW);g=AppointmentGateway(m,p,clock=lambda:NOW);m.appointments=g
    cid=m.db.create_inbound('CA_fixture','+12025550111',15,10,'concierge',True);m.transition(cid,'in_progress')
    with m.db.connect() as db:db.execute('update calls set created_at=?,started_at=? where call_id=?',(NOW.isoformat(),NOW.isoformat(),cid))
    for i,(who,text) in enumerate([
        ('callee','I would like the first available appointment.'),
        ('assistant','What is your full name?'),('callee','Casey Example.'),
        ('assistant','What is the appointment for?'),('callee','Course guidance.'),
        ('assistant','How long would you like?'),('callee','Fifteen minutes.'),
        ('assistant','Which room?'),('callee','Office A.')]):speech(m,cid,who,text,i*1000)
    args=dict(first_name='Casey',last_name='Example',purpose='course guidance',duration_minutes=15,arrangement='Office A')
    return client,m,p,g,cid,args


def test_voice_first_available_offers_once_then_books_on_contextual_agreement(service,tmp_path):
    from app.calling.live_session import MediaBridge
    from tests.test_call_updates import UpdateConnection
    client,m,p,g,cid,args=first_call(service,tmp_path)
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_FIRST');m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation({'name':'find_first_available_slot','arguments':json.dumps(args)})
        assert result['decision']=='available' and result['readback_queued'] and not result['consent_observed']
        assert 'readback' not in result and result['reservation'] is False and p.writes==0
        assert len(con.updates)==1 and 'October 26th, 10:00 AM. Is that correct?' in con.updates[0][1]['content']
        book={'name':'book_appointment','arguments':json.dumps(dict(draft_id=result['draft_id'],check_id=result['check_id'],idempotency_key='one'))}
        # The initial flexible preference does not authorize an unseen proposal.
        denied=await bridge.tools.operation(book)
        assert denied['state']=='needs_consent' and not denied['calendar_write_attempted'] and p.writes==0
        command=con.updates[0][1]
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
        speech(m,cid,'assistant','October 26th, 10:00 AM. Is that correct?',20000)
        speech(m,cid,'callee','That works for me.',21000)
        booked=await bridge.tools.operation(book)
        assert booked['state']=='confirmed' and p.writes==1
        assert (await bridge.tools.operation(book))['operation_id']==booked['operation_id'] and p.writes==1
        assert len(con.updates)==2 and 'Confirmed. October 26th, 10:00 AM.' in con.updates[-1][1]['content']
        assert not m.db.get(cid)['supervisor_requests']
    client.portal.call(exercise)


@pytest.mark.parametrize('change',['busy','incomplete','hangup'])
def test_exact_check_after_search_must_still_pass_before_offering(service,tmp_path,change):
    client,m,p,g,cid,args=first_call(service,tmp_path)
    original=p.read_availability
    async def scan(interval):
        coverage=await original(interval)
        if change=='busy':p.events['busy']=CalendarEvent(EventIdentity(p.identity,'busy'),'r',SLOT)
        if change=='incomplete':p.complete=False
        if change=='hangup':m.transition(cid,'completed')
        return coverage
    p.read_availability=scan
    if change=='hangup':
        with pytest.raises(ValueError):client.portal.call(g.first_available,cid,args)
    else:
        result=client.portal.call(g.first_available,cid,args)
        assert result['decision']==('busy' if change=='busy' else 'unknown')
        assert not result['readback_queued']
    with m.db.connect() as db:assert db.execute('select count(*) from call_updates where call_id=?',(cid,)).fetchone()[0]==0
    assert p.writes==0


def test_mcp_search_requires_existing_read_scope_and_returns_same_bound_proposal(service,tmp_path,monkeypatch):
    from app.mcp.server import create_mcp
    from app.appointments.mcp_auth import security_schemes
    client,m,p,g,cid,args=first_call(service,tmp_path);m.settings.oauth_database_path=tmp_path/'oauth.sqlite3'
    mcp,oauth=create_mcp(m)
    async def exercise():
        tools={t.name:t for t in await mcp.list_tools()}
        assert tools['find_first_available_slot'].model_dump(by_alias=True)['_meta']['requiredScope']=='phone.appointments.read'
        assert security_schemes('find_first_available_slot')[0]['scopes']==['phone.calls','phone.appointments.read']
        monkeypatch.setattr('app.appointments.mcp.get_access_token',lambda:SimpleNamespace(scopes=['phone.calls']))
        with pytest.raises(Exception,match='find_first_available_slot'):
            await mcp.call_tool('find_first_available_slot',{'call_id':cid,'request':args})
        with g.store.connect() as db:assert db.execute('select count(*) from appointment_drafts').fetchone()[0]==0
        monkeypatch.setattr('app.appointments.mcp.get_access_token',lambda:SimpleNamespace(scopes=['phone.calls','phone.appointments.read']))
        response=await mcp.call_tool('find_first_available_slot',{'call_id':cid,'request':args})
        assert 'first_available_in_search_window' in str(response) and p.writes==0
    client.portal.call(exercise)


def test_google_search_reads_all_pages_without_private_titles_or_early_busy_exit():
    import httpx
    from tests.test_appointments_google import adapter,office,health
    calls=[]
    def handle(request):
        calls.append(request)
        assert request.method=='GET'
        if request.url.host=='mirror.test':return httpx.Response(200,json=health())
        assert 'summary' not in request.url.params['fields']
        if not request.url.params.get('pageToken'):
            return httpx.Response(200,json={'items':[{**office(),'id':'private-busy'}],'nextPageToken':'more'})
        return httpx.Response(200,json={'items':[office()]})
    async def exercise():
        p=adapter(handle)
        coverage=await p.read_availability(Interval(NOW,NOW+timedelta(days=14),SLOT.zone))
        assert coverage.complete and len(coverage.events)==2 and len(calls)==3
        assert not any(e.owner_label for e in coverage.events)
        await p.http.aclose()
    asyncio.run(exercise())


def test_search_follows_local_office_hours_across_dst(tmp_path):
    svc,store,p,d,state,now=setup(tmp_path)
    next_monday=SLOT.start+timedelta(days=7,hours=1)  # 10 a.m. becomes 15:00 UTC after DST.
    p.office=replace(p.office,interval=Interval(next_monday,next_monday+timedelta(hours=2),SLOT.zone))
    result=asyncio.run(svc.find_slot('call-1','request-1',15))
    assert result['interval'].start==next_monday
    assert svc.policy.office_hours(result['interval']) and p.writes==0


@pytest.mark.parametrize('mode',['outbound','message_only','permission','closing'])
def test_voice_search_keeps_the_existing_live_session_boundary(service,tmp_path,mode):
    from app.calling.live_session import MediaBridge
    from tests.test_call_updates import UpdateConnection
    client,m,p,g,cid,args=first_call(service,tmp_path)
    async def exercise():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_GUARDED_FIRST');m.attach_bridge(cid,bridge)
        with m.db.connect() as db:
            if mode=='outbound':db.execute("update calls set direction='outbound' where call_id=?",(cid,))
            if mode=='message_only':db.execute("update calls set inbound_mode='messages' where call_id=?",(cid,))
            if mode=='permission':db.execute('update calls set inbound_booking_allowed=0 where call_id=?',(cid,))
        if mode=='closing':bridge.closing=True
        with pytest.raises(ValueError):await bridge.tools.operation({'name':'find_first_available_slot','arguments':json.dumps(args)})
        with g.store.connect() as db:assert db.execute('select count(*) from appointment_drafts').fetchone()[0]==0
        assert p.writes==0
    client.portal.call(exercise)
