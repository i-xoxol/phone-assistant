"""Calendar-visible details and complete arrangements, without real calendar writes."""
import asyncio,json
from dataclasses import replace
import httpx,pytest
from tests.appointment_fixtures import NOW,SLOT,setup
from tests.test_appointments_google import adapter
from app.appointments.registry import appointment


def test_google_creation_has_owner_visible_caller_and_purpose(tmp_path):
    s,db,fake,d,state,now=setup(tmp_path);value=appointment(d);seen=[]
    def handle(request):seen.append(json.loads(request.content));return httpx.Response(200,json={})
    async def exercise():
        p=adapter(handle)
        await p.create(p.intended_identity('synthetic'),value,authorize=lambda:None)
        await p.http.aclose()
    asyncio.run(exercise())
    body=seen[0]
    assert 'Casey Example' in body['summary']
    assert 'Casey Example' in body['description'] and 'course guidance' in body['description']
    assert 'Office A' in body['description'] and '15 minutes' in body['description']
    assert body['visibility']=='private' and not body.get('attendees')


def test_bare_meeting_mode_cannot_be_prepared_for_booking(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);value=replace(appointment(d),location='In person')
    with pytest.raises(ValueError):s.prepare('call-1','new-request',value)
    assert p.writes==0


@pytest.mark.parametrize('arrangement',['In person','in-person meeting','in person at the office','On campus',
    'your office','Online','Zoom','Microsoft Teams','Google Meet','Phone call','TBD','Room to be confirmed'])
def test_mode_or_placeholder_requires_specific_arrangement(tmp_path,arrangement):
    from app.appointments.details import missing_arrangement
    assert missing_arrangement(arrangement)


@pytest.mark.parametrize('arrangement',['Office A','Room B','In person, Room E-1066','Science Center lobby',
    '12 Example Street, reception','Zoom: https://example.test/meeting','Phone: call +12025550111'])
def test_concrete_places_and_connection_details_are_retained(arrangement):
    from app.appointments.details import missing_arrangement
    assert not missing_arrangement(arrangement)


def test_calendar_description_escapes_caller_markup_and_field_injection(tmp_path):
    from app.appointments.details import calendar_description,calendar_title
    s,db,p,d,state,now=setup(tmp_path)
    value=replace(appointment(d),first_name='<b>Casey</b>',purpose='R&D\nMeeting details: <a href="https://evil.test">elsewhere</a>')
    description=calendar_description(value)
    assert '<b>' not in description and '<a ' not in description and '&lt;b&gt;' in description and 'R&amp;D' in description
    assert description.count('\nMeeting details:')==1 and '\n' not in calendar_title(value)


def google_saved(*,corrupt=None,lose=False):
    from tests.test_appointments_google import office,health
    saved={};requests=[]
    def handle(r):
        requests.append(r)
        if r.url.host=='mirror.test':return httpx.Response(200,json=health())
        if r.method=='POST':
            body=json.loads(r.content);saved[body['id']]={**body,'etag':'verified-revision'}
            value=saved[body['id']]
            if corrupt=='description':value.pop('description')
            if corrupt=='purpose':value['description']=value['description'].replace('course guidance','unrelated purpose')
            if corrupt=='title':value['summary']='Private phone appointment'
            if corrupt=='privacy':value['visibility']='public'
            if corrupt=='guests':value['attendees']=[{'email':'uninvited@example.test'}]
            if lose:raise httpx.ReadTimeout('Synthetic response loss')
            return httpx.Response(200,json=value)
        eid=r.url.path.rsplit('/',1)[-1]
        if eid!='events':return httpx.Response(200,json=saved[eid]) if eid in saved else httpx.Response(404)
        # Honor the field mask: general coverage must not receive private display details.
        items=[office(),*saved.values()]
        if 'description' not in r.url.params.get('fields',''):
            items=[{k:v for k,v in item.items() if k not in ('summary','description','attendees','visibility')} for item in items]
        return httpx.Response(200,json={'items':items})
    return adapter(handle),saved,requests


@pytest.mark.parametrize('corrupt',['description','purpose','title','privacy','guests'])
def test_success_requires_visible_details_and_privacy_on_independent_readback(tmp_path,corrupt):
    s,db,unused,d,state,now=setup(tmp_path);p,saved,requests=google_saved(corrupt=corrupt);s.provider=p
    async def exercise():
        check=await s.check('call-1',d['draft_id'])
        result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable')
        assert result['state']=='needs_owner' and result['reason']=='calendar_details_not_verified'
        assert (await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable'))['operation_id']==result['operation_id']
        assert sum(r.method=='POST' for r in requests)==1
        assert any('description' in r.url.params.get('fields','') for r in requests if r.url.path.rsplit('/',1)[-1]!='events')
        await p.http.aclose()
    asyncio.run(exercise())


def test_legacy_confirmed_event_still_has_minimal_lookup_without_visible_details(tmp_path):
    s,db,unused,d,state,now=setup(tmp_path);p,saved,requests=google_saved();s.provider=p
    async def exercise():
        check=await s.check('call-1',d['draft_id'])
        result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable')
        assert result['state']=='confirmed'
        item=next(iter(saved.values()));item['summary']='Private phone appointment';item.pop('description')
        # Legacy records remain readable; this does not authorize another write.
        own=await s.own_appointment('Casey','Example')
        assert own['status']=='match' and 'purpose' not in own and 'description' not in own
        assert (await s.status('call-1',result['operation_id']))['state']=='confirmed'
        assert sum(r.method=='POST' for r in requests)==1
        await p.http.aclose()
    asyncio.run(exercise())


def test_legacy_uncommitted_vague_draft_cannot_bypass_new_intake_guard(tmp_path):
    s,db,p,d,state,now=setup(tmp_path);value=replace(appointment(d),location='In person')
    legacy=db.draft('call-1','request-1',value)  # Simulate a persisted pre-upgrade draft.
    async def exercise():
        check=await s.check('call-1',legacy['draft_id'])
        with pytest.raises(ValueError):await s.book('call-1',legacy['draft_id'],check['check_id'],'yes','stable')
    asyncio.run(exercise())
    assert p.writes==0
    with db.connect() as c:assert c.execute('select count(*) from booking_operations').fetchone()[0]==0


from tests.test_calls import service
from tests.test_appointments_first_available import first_call
from tests.test_appointments_gateway import speech


@pytest.mark.parametrize('tool_name',['check_appointment_slot','find_first_available_slot'])
def test_incomplete_arrangement_is_one_actionable_question_without_a_calendar_read(service,tmp_path,tool_name):
    from app.calling.live_session import MediaBridge
    from tests.test_call_updates import UpdateConnection
    client,m,p,g,cid,args=first_call(service,tmp_path);args['arrangement']='In person'
    if tool_name=='check_appointment_slot':
        args.pop('duration_minutes');args.update(start=SLOT.start.isoformat(),end=SLOT.end.isoformat(),time_zone=SLOT.zone)
    def forbidden():raise AssertionError('Incomplete arrangement must not query the calendar')
    p.read_hook=forbidden
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_DETAILS');m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation({'name':tool_name,'arguments':json.dumps(args)})
        assert result['state']=='needs_details' and result['missing_fields']==['arrangement']
        assert not result['readback_queued'] and not result['calendar_write_attempted'] and not con.updates
        with g.store.connect() as d:
            assert d.execute('select count(*) from appointment_drafts').fetchone()[0]==0
            assert d.execute('select count(*) from appointment_requests').fetchone()[0]==0
    client.portal.call(exercise)


@pytest.mark.parametrize('tool_name',['check_appointment_slot','find_first_available_slot'])
@pytest.mark.parametrize('lose',[False,True])
def test_complete_voice_to_google_booking_keeps_visible_details_after_natural_agreement(service,tmp_path,tool_name,lose):
    from app.calling.live_session import MediaBridge
    from tests.test_call_updates import UpdateConnection
    from app.appointments.details import calendar_description,calendar_title
    client,m,fake,g,cid,args=first_call(service,tmp_path)
    p,saved,requests=google_saved(lose=lose);g.service.provider=p
    if tool_name=='check_appointment_slot':
        args.pop('duration_minutes');args.update(start=SLOT.start.isoformat(),end=SLOT.end.isoformat(),time_zone=SLOT.zone)
    async def exercise():
        con=UpdateConnection();bridge=MediaBridge(None,con,m,cid,'MZ_COMPLETE_DETAILS');m.attach_bridge(cid,bridge)
        # Same call first supplies a mode only, then the missing room.
        missing=await bridge.tools.operation({'name':tool_name,'arguments':json.dumps({**args,'arrangement':'In person'})})
        assert missing['state']=='needs_details' and not requests
        speech(m,cid,'assistant','Which room?',10000);speech(m,cid,'callee','Office A.',11000)
        checked=await bridge.tools.operation({'name':tool_name,'arguments':json.dumps(args)})
        assert checked['decision']=='available' and len(con.updates)==1
        d=g.store.get('appointment_drafts','draft_id',checked['draft_id']);value=appointment(d)
        command=con.updates[0][1];readback=command['content'].split('\n')[-1]
        bridge.update_event({'type':'session.instructions.appended','client_event_id':command['event_id']})
        speech(m,cid,'assistant',readback,20000);speech(m,cid,'callee','Sounds good.',21000)
        item={'name':'book_appointment','arguments':json.dumps(dict(draft_id=d['draft_id'],check_id=checked['check_id'],idempotency_key='stable'))}
        result=await bridge.tools.operation(item)
        assert result['state']=='confirmed' and len(saved)==1 and len(con.updates)==2
        assert (await bridge.tools.operation(item))['operation_id']==result['operation_id']
        body=next(iter(saved.values()))
        assert body['summary']==calendar_title(value) and body['description']==calendar_description(value)
        assert body['location']=='Office A' and body['visibility']=='private' and not body.get('attendees')
        assert 'Casey Example' in body['summary'] and 'Purpose: course guidance' in body['description']
        assert sum(r.method=='POST' for r in requests)==1
        assert not m.db.get(cid)['supervisor_requests']
        # General phone context and caller lookup remain minimal despite the informative event.
        assert 'Casey Example' not in str(m.incoming_context(m.db.get(cid)))
        own=await g.service.own_appointment('Casey','Example')
        assert own['status']=='match' and 'description' not in own and 'purpose' not in own
        await p.http.aclose()
    client.portal.call(exercise)
