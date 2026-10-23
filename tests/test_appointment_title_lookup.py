"""Caller name anywhere in any meeting title; fresh reads, no booking side effects."""
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import httpx
import pytest
from app.appointments import name_match
from app.appointments.contracts import Coverage,ProviderFault
from app.appointments.name_match import NameChoice
from app.appointments.title_lookup import TitleLookup
from app.calling.live_session import MediaBridge
from tests.appointment_fixtures import NOW,SLOT
from tests.test_appointment_relaxed_name_resolution import model,rows
from tests.test_appointments_google import adapter,health,office
from tests.test_appointments_gateway import prepare,speech
from tests.test_call_updates import UpdateConnection
from tests.test_calls import service


@pytest.fixture
def anyio_backend():return 'asyncio'


def raw_event(title='Example Jr, Casey D. - Office hours (Online) [Work]'):
    return {**office(),'id':'existing-work','etag':'work-v1','status':'confirmed',
        'summary':title,'location':'Microsoft Teams Meeting',
        'end':{'dateTime':SLOT.end.isoformat(),'timeZone':SLOT.zone}}


def choice():return NameChoice(candidate=0,first_quote='Kasey',last_quote='Exammple')


@pytest.mark.anyio
@pytest.mark.parametrize('title',[
    'Example Jr, Casey D. - Office hours (Online) [Work]',
    'Schedule review with Casey Example',
    'Unrelated prefix ' * 14 + 'Casey Example: follow-up',
])
async def test_any_title_can_match_without_participant_metadata_or_index(title):
    raw=raw_event(title);requests=[]
    def handle(r):
        requests.append(r);assert r.method=='GET'
        if r.url.host=='mirror.test':return httpx.Response(200,json=health())
        assert 'summary' in r.url.params['fields'] and 'description' not in r.url.params['fields']
        return httpx.Response(200,json=raw if r.url.path.endswith('/existing-work') else {'items':[raw,office()]})
    p=adapter(handle);resolver=AsyncMock(return_value=choice())
    assert p.normalize(raw).disclosure=='unknown' and not p.normalize(raw).participant_first
    result=await TitleLookup(p,resolver,lambda:NOW).find(rows(('callee','Earlier I said Kasey Exammple.')))
    assert result['status']=='match' and result['location']=='Microsoft Teams Meeting'
    assert set(result)=={'status','start','end','zone','location','duration_seconds'}
    assert resolver.await_args.args[1]==[title] and len(requests)==3
    await p.http.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize('failure',[
    'deleted','cancelled','revision','title','time','location','expired','incomplete','future_clock',
    'unrelated_quote','out_of_range','provider_failure','model_failure',
])
async def test_stale_reads_and_ungrounded_selections_never_disclose_details(failure):
    p=adapter(lambda r:httpx.Response(200,json={}))
    event=replace(p.normalize(raw_event()),owner_label=raw_event()['summary'])
    now=[NOW];coverage=Coverage(SLOT,NOW,NOW+timedelta(seconds=30),True,(event,))
    async def scan(window):
        return replace(coverage,interval=window,complete=failure!='incomplete',
            checked_at=NOW+timedelta(seconds=1) if failure=='future_clock' else NOW)
    async def reread(identity):
        if failure=='provider_failure':raise ProviderFault('unavailable')
        if failure=='expired':now[0]+=timedelta(seconds=31)
        if failure=='deleted':return None
        return replace(event,**{
            'cancelled':{'state':'cancelled'},'revision':{'revision':'work-v2'},
            'title':{'owner_label':'Different person'},'location':{'location':'Different place'},
            'time':{'interval':replace(SLOT,start=SLOT.start+timedelta(minutes=1))},
        }.get(failure,{}))
    p.read_lookup_titles=scan;p.read_lookup_event=reread
    c=choice()
    if failure=='unrelated_quote':c=NameChoice(candidate=0,first_quote='Assistant',last_quote='Only')
    if failure=='out_of_range':c=NameChoice(candidate=99,first_quote='Kasey',last_quote='Exammple')
    resolver=AsyncMock(return_value=c,side_effect=ProviderFault('unavailable') if failure=='model_failure' else None)
    result=await TitleLookup(p,resolver,lambda:now[0]).find(rows(('callee','Kasey Exammple'),('assistant','Assistant Only')))
    assert result['status']=='stale' and not {'start','end','location','title','candidates'}&set(result)
    await p.http.aclose()


@pytest.mark.anyio
async def test_title_matcher_uses_priority_luna_caller_quotes_and_no_action_tools(monkeypatch):
    from app.config import Settings
    parse=model(monkeypatch,choice())
    titles=['Example Jr, Casey D. - Office hours (Online) [Work]']
    c=await name_match.resolve_title(Settings(_env_file=None),rows(('callee','Kasey Exammple'),('assistant','Assistant-only name')),titles)
    assert c==choice()
    args=parse.await_args.kwargs
    assert args['model']=='gpt-6-luna' and args['service_tier']=='priority' and args['store'] is False
    assert 'tools' not in args and 'Assistant-only' not in args['input']
    assert json.loads(args['input'])['candidates']==[dict(candidate=0,title=titles[0])]
    parse.return_value=SimpleNamespace(output_parsed=NameChoice(candidate=0,first_quote='Invented',last_quote='Name'))
    assert await name_match.resolve_title(Settings(_env_file=None),rows(('callee','Kasey Exammple')),titles) is None


@pytest.mark.anyio
@pytest.mark.parametrize('mode',['too_many','too_long','no_match','no_caller'])
async def test_bounded_title_search_never_reads_an_unselected_event(mode):
    p=adapter(lambda r:httpx.Response(200,json={}))
    event=replace(p.normalize(raw_event()),owner_label='Casey Example')
    events=(event,)*501 if mode=='too_many' else (replace(event,owner_label='x'*64001),) if mode=='too_long' else (event,)
    async def scan(window):return Coverage(window,NOW,NOW+timedelta(seconds=30),True,events)
    p.read_lookup_titles=AsyncMock(side_effect=scan);p.read_lookup_event=AsyncMock()
    resolver=AsyncMock(return_value=None)
    result=await TitleLookup(p,resolver,lambda:NOW).find(rows(('assistant' if mode=='no_caller' else 'callee','Kasey Exammple')))
    if mode.startswith('too_'):
        assert result['status']=='stale' and result['reason']=='title_search_limit'
        resolver.assert_not_awaited()
    else:assert result is None
    p.read_lookup_event.assert_not_awaited()
    if mode=='no_caller':p.read_lookup_titles.assert_not_awaited()
    await p.http.aclose()


@pytest.mark.parametrize('mode',['normal','ambiguous','ambiguous_changed','hangup','pending_audio'])
def test_voice_lookup_falls_back_to_titles_once_without_creating_booking(service,tmp_path,mode):
    client,m,p,g,cid,_=prepare(service,tmp_path)
    m.transition(cid,'completed');cid=m.db.create_inbound('CA_TITLE_LOOKUP','+12025550101',3,10,'concierge',True)
    m.transition(cid,'in_progress');m.settings.appointments_enabled=True
    speech(m,cid,'callee','I am Kasey Exammple.',1000)
    p.capabilities=replace(p.capabilities,title_lookup=True)
    from app.appointments.contracts import CalendarEvent,EventIdentity,ChangePage
    event=CalendarEvent(EventIdentity(p.identity,'work-booking'),'r1',SLOT,
        owner_label='Example Jr, Casey D. - Unstructured meeting [Work]',location='Microsoft Teams Meeting')
    if mode.startswith('ambiguous'):
        # The prior event was cancelled externally, then a new booking with the
        # same name was immediately persisted before the next index poll.
        indexed=replace(event,participant_first='Kasey',participant_last='Exammple',disclosure='ordinary')
        previous=replace(indexed,identity=EventIdentity(p.identity,'previous-booking'))
        g.store.ingest(ChangePage((previous,indexed),'before-cancellation-poll',True),p.identity,NOW,dry_run=False)
        assert g.store.own_appointment('Kasey','Exammple',NOW)['status']=='ambiguous'
    async def scan(window):return Coverage(window,NOW,NOW+timedelta(seconds=30),True,(event,))
    async def reread(identity):
        if mode=='hangup':m.transition(cid,'completed')
        if mode=='pending_audio':m.caller_activity[cid]={'last_speech_ms':999999}
        return replace(event,revision='changed') if mode=='ambiguous_changed' else event
    p.read_lookup_titles=scan;p.read_lookup_event=reread
    g.title_lookup.resolver=AsyncMock(return_value=choice());g.name_resolver=AsyncMock()
    g.lookup_wait_seconds=.2;g.lookup_settle_seconds=.001
    async def check():
        bridge=MediaBridge(None,UpdateConnection(),m,cid,'MZ_TITLES');m.attach_bridge(cid,bridge)
        result=await bridge.tools.operation({'name':'get_my_appointment','arguments':json.dumps(dict(first_name='Kasey',last_name='Exammple'))})
        if mode in ('normal','ambiguous'):
            assert result['status']=='match' and 'Microsoft Teams Meeting' in result['spoken_answer']
            assert 'Example Jr' not in str(result) and g.title_lookup.resolver.await_count==1
        else:assert result['status']!='match' and not {'start','end','location'}&set(result)
        if mode=='ambiguous_changed':assert result['status']=='stale'
        g.name_resolver.assert_not_awaited()
        assert p.writes==0
        with g.store.connect() as db:
            assert db.execute('SELECT count(*) FROM booking_operations').fetchone()[0]==0
    client.portal.call(check)


@pytest.mark.parametrize('start,time',[
    ('2026-10-26T14:15:00+00:00','10:15 a.m.'),
    ('2026-10-27T01:15:00+00:00','9:15 p.m.'),
])
def test_lookup_spoken_weekday_uses_the_appointment_local_date(start,time):
    from app.appointments.gateway import lookup_voice_result
    result=lookup_voice_result({'status':'match','start':start,'zone':'America/New_York','location':'Office A'})
    assert result['spoken_answer']==f'Your appointment is Monday, October 26 at {time}, Office A.'
    assert 'weekday' in result['instruction'] and '2026' not in result['spoken_answer']
