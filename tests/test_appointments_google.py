from datetime import timedelta
from dataclasses import replace
import httpx,pytest
from tests.appointment_fixtures import NOW,SLOT,setup
from app.appointments.google import GoogleCalendarAdapter,MirrorHealth
from app.appointments.contracts import CalendarIdentity,ProviderFault
from app.appointments.policy import BookingPolicy

@pytest.fixture
def anyio_backend():return 'asyncio'

def office():return {'id':'trusted-office','etag':'"r1"','start':{'dateTime':SLOT.start.isoformat(),'timeZone':SLOT.zone},
    'end':{'dateTime':(SLOT.start+timedelta(hours=2)).isoformat(),'timeZone':SLOT.zone}}

async def token():return 'synthetic-test-token'

def adapter(handler,**kwargs):
    http=httpx.AsyncClient(transport=httpx.MockTransport(handler),follow_redirects=False)
    mirror=MirrorHealth(http,'http://mirror.test/api/status',lambda:NOW,verified_window=True)
    return GoogleCalendarAdapter(http,token,CalendarIdentity('google','account-A','primary'),mirror,lambda:NOW,
        office_ids=['trusted-office'],**kwargs)

def health():return {'state':'healthy','timerActive':True,'windowDaysPast':30,'windowDaysFuture':365,
    'lastSuccess':{'outcome':'success','timestamp':NOW.timestamp(),'counts':{'outlookEvents':1,'googleEventsScanned':2}}}

@pytest.mark.anyio
async def test_google_exact_interval_and_mirror_gate():
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,json=health() if request.url.host=='mirror.test' else {'items':[office()]})
    p=adapter(handle);coverage=await p.read_interval(SLOT)
    assert coverage.complete and BookingPolicy().decide(SLOT,coverage,NOW)[0]=='available'
    assert requests[0].url.params['timeMin']==SLOT.start.isoformat()
    assert requests[0].url.params['singleEvents']=='true'
    assert 'summary' not in requests[0].url.params['fields']
    await p.http.aclose()

@pytest.mark.anyio
@pytest.mark.parametrize('mutation,reason',[
    ({'state':'attention'},'mirror_stale_or_failed'),({'timerActive':False},'mirror_stale_or_failed'),
    ({'windowDaysFuture':14},'mirror_window_changed'),
    ({'lastSuccess':{'outcome':'success','timestamp':NOW.timestamp()-500}},'mirror_stale_or_failed'),
    ({'lastSuccess':{'outcome':'success','timestamp':NOW.timestamp()}},'mirror_completeness_unknown')])
async def test_stale_or_partial_aggregate_is_unknown(mutation,reason):
    def handle(request):return httpx.Response(200,json={**health(),**mutation} if request.url.host=='mirror.test' else {'items':[office()]})
    p=adapter(handle);coverage=await p.read_interval(SLOT)
    assert not coverage.complete and coverage.reason==reason
    assert BookingPolicy().decide(SLOT,coverage,NOW)[0]=='unknown'
    await p.http.aclose()

@pytest.mark.anyio
async def test_known_conflict_does_not_wait_for_mirror_or_next_page():
    calls=[]
    def handle(request):
        calls.append(request.url.host)
        assert request.url.host!='mirror.test'
        busy={**office(),'id':'private-commitment','summary':'must never be disclosed'}
        return httpx.Response(200,json={'items':[busy],'nextPageToken':'more'})
    p=adapter(handle);result=await p.read_interval(SLOT)
    assert BookingPolicy().decide(SLOT,result,NOW)[0]=='busy' and len(calls)==1
    assert 'summary' not in repr(result)
    await p.http.aclose()

@pytest.mark.anyio
async def test_partial_pagination_never_reports_free():
    p=adapter(lambda r:httpx.Response(200,json={'items':[],'nextPageToken':'more'}),max_pages=2)
    result=await p.read_interval(SLOT)
    assert not result.complete and result.reason=='incomplete_pagination'
    await p.http.aclose()

@pytest.mark.anyio
async def test_write_is_private_distinct_owned_and_no_invitations(tmp_path):
    seen=[]
    def handle(request):seen.append(request);return httpx.Response(200,json={})
    p=adapter(handle);s,db,fake,d,state,now=setup(tmp_path)
    from app.appointments.registry import appointment
    identity=p.intended_identity('one-operation')
    await p.create(identity,appointment(d),authorize=lambda:None)
    import json
    body=json.loads(seen[0].content)
    assert body['visibility']=='private' and body['id']==identity.event
    assert 'attendees' not in body and 'conferenceData' not in body and seen[0].url.params['sendUpdates']=='none'
    assert body['extendedProperties']['private']['phoneAgentOwner']=='phone-assistant'
    assert 'bridgeName' not in body['extendedProperties']['private']
    await p.http.aclose()

def test_provenance_not_title_or_icaluid_identity():
    p=adapter(lambda r:httpx.Response(200,json={}),mirror_source=CalendarIdentity('outlook','verified-account','verified-calendar'))
    raw={**office(),'id':'mirror-event','summary':'Office Hours','iCalUID':'not-an-identity',
        'extendedProperties':{'private':{'bridgeName':'shu-outlook','outlookEventId':'opaque-source',
            'outlookICalUID':'series-uid','outlookLastModified':'2026-10-20T00:00:00Z'}}}
    event=p.normalize(raw)
    assert event.alias_verified and event.aliases[0].event=='opaque-source'
    assert event.kind=='commitment' and not event.participant_first

def test_all_day_and_recurrence_tombstones():
    p=adapter(lambda r:httpx.Response(200,json={}))
    e=p.normalize({'id':'all-day','etag':'1','start':{'date':'2026-11-01'},'end':{'date':'2026-11-02'}})
    assert e.all_day[0].isoformat()=='2026-11-01' and (e.interval.end-e.interval.start).total_seconds()==25*3600
    gone=p.normalize({'id':'moved-instance','status':'cancelled','originalStartTime':{'dateTime':'2026-10-26T10:00:00-04:00'}})
    assert gone.state=='cancelled' and gone.identity.occurrence=='2026-10-26T10:00:00-04:00'

@pytest.mark.anyio
async def test_cursor_pagination_and_expiry_are_explicit():
    calls=[]
    def handle(r):
        calls.append(r)
        if r.url.params.get('syncToken')=='expired':return httpx.Response(410,json={})
        if not r.url.params.get('pageToken'):return httpx.Response(200,json={'items':[],'nextPageToken':'p2'})
        return httpx.Response(200,json={'items':[],'nextSyncToken':'next-opaque'})
    p=adapter(handle);page=await p.changes('original-opaque')
    assert page.cursor=='next-opaque' and page.complete
    assert all(r.url.params['syncToken']=='original-opaque' and 'timeMin' not in r.url.params for r in calls)
    with pytest.raises(ProviderFault,match='cursor_expired'):await p.changes('expired')
    await p.http.aclose()

@pytest.mark.anyio
async def test_healthy_mirror_does_not_promise_zero_lag():
    p=adapter(lambda r:httpx.Response(200,json=health() if r.url.host=='mirror.test' else {'items':[office()]}))
    # A work commitment created since the last completed mirror is not visible yet.
    result=await p.read_interval(SLOT)
    assert result.complete and result.reason=='verified_with_mirror_lag'
    await p.http.aclose()
