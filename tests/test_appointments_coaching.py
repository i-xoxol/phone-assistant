import asyncio,json,time
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import httpx,pytest
from app.appointments.coaching import owner_schedule
from app.appointments.contracts import CalendarEvent,EventIdentity,Coverage
from app.calling.realtime_coaching import RealtimeCoachSession
from app.config import Settings
from tests.appointment_fixtures import setup,NOW,SLOT
from tests.test_appointments_google import adapter,office,health
from tests.test_realtime_coaching import Connection,factory
from tests.test_calls import service

@pytest.fixture
def anyio_backend():return 'asyncio'

def query(view='schedule'):
    return {'start':SLOT.start.isoformat(),'end':(SLOT.start+timedelta(days=1)).isoformat(),
        'time_zone':SLOT.zone,'view':view}

@pytest.mark.anyio
async def test_google_private_agenda_is_separate_complete_and_no_attendees():
    seen=[]
    def handle(r):
        seen.append(r)
        return httpx.Response(200,json=health() if r.url.host=='mirror.test' else {
            'items':[{**office(),'id':'private-event','summary':'Owner private meeting'}]})
    p=adapter(handle);gateway=SimpleNamespace(service=SimpleNamespace(provider=p),clock=lambda:NOW)
    try:
        result=await owner_schedule(gateway,query(),lambda:True)
        assert result['status']=='current' and result['appointments'][0]['label']=='Owner private meeting'
        assert result['private_owner_only'] and 'summary' in seen[0].url.params['fields']
        assert 'attendees' not in str(seen[0].url) and 'description' not in str(seen[0].url)
        busy=await owner_schedule(gateway,query('free_busy'),lambda:True)
        assert 'Owner private meeting' not in str(busy) and 'label' not in str(busy)
    finally:await p.http.aclose()

@pytest.mark.anyio
@pytest.mark.parametrize('fault',['revoked','stale','partial','horizon','range','selector','unsupported'])
async def test_calendar_lookup_fail_closed(tmp_path,fault):
    s,db,p,d,state,now=setup(tmp_path);p.capabilities=replace(p.capabilities,owner_agenda=fault!='unsupported')
    async def read(interval):return Coverage(interval,NOW,NOW+timedelta(seconds=-1 if fault=='stale' else 30),fault!='partial')
    p.read_agenda=read
    args=query()
    if fault=='horizon':args['start']=(NOW+timedelta(days=95)).isoformat();args['end']=(NOW+timedelta(days=96)).isoformat()
    if fault=='range':args['end']=(NOW+timedelta(days=20)).isoformat()
    if fault=='selector':args['calendar_id']='other-person'
    result=await owner_schedule(SimpleNamespace(service=s,clock=lambda:NOW),args,lambda:fault!='revoked')
    assert result['status'] in ('unknown','unavailable') and 'appointments' not in result

class ToolConnection(Connection):
    async def send(self,data):
        if data['type']=='response.create' and self.counter==0:
            self.sent.append(data);self.counter+=1
            await self.queue.put({'type':'response.created','response':{'id':'lookup-response','metadata':data['response']['metadata']}})
            await self.queue.put({'type':'response.done','response':{'id':'lookup-response','status':'completed',
                'output':[{'type':'function_call','name':'get_owner_schedule','call_id':'lookup-1','arguments':json.dumps(query())}]}})
        else:await super().send(data)

@pytest.mark.anyio
async def test_native_realtime_can_call_calendar_then_speak_and_next_turn_has_no_stale_result():
    connection=ToolConnection();session=RealtimeCoachSession(Settings(_env_file=None),lambda *a,**k:None,factory(connection))
    looked=[];packets=[]
    async def lookup(args):looked.append(args);return {'status':'current','private_owner_only':True,'appointments':[{'label':'owner-only synthetic'}]}
    session.calendar_lookup=lookup
    async def audio(raw):packets.append(raw)
    try:
        answer=await session.ask({},audio)
        assert len(looked)==1 and answer['audio_bytes']==320 and packets
        requests=[e['response'] for e in connection.sent if e['type']=='response.create']
        assert all(r['conversation']=='none' for r in requests)
        output=requests[-1]['input'][-1]
        assert output['type']=='function_call_output' and output['call_id']=='lookup-1'
        assert 'owner-only synthetic' in output['output']
        await session.ask({'new':'cue'},audio)
        assert 'owner-only synthetic' not in json.dumps(connection.sent[-1])
        assert not any(t['name']=='book_appointment' for t in requests[0]['tools'])
    finally:await session.close()

@pytest.mark.anyio
async def test_hangup_during_lookup_prevents_late_calendar_response():
    connection=ToolConnection();session=RealtimeCoachSession(Settings(_env_file=None),lambda *a,**k:None,factory(connection))
    started=asyncio.Event();release=asyncio.Event();packets=[]
    async def lookup(args):started.set();await release.wait();return {'status':'current'}
    session.calendar_lookup=lookup
    async def audio(raw):packets.append(raw)
    task=asyncio.create_task(session.ask({},audio))
    try:
        await started.wait();await session.cancel();release.set()
        with pytest.raises(asyncio.CancelledError):await task
        await asyncio.sleep(.01)
        assert connection.counter==1 and not packets
    finally:await session.close()

def test_calendar_callback_is_bound_to_accepted_owner_coaching_and_revoked_on_pause(service,tmp_path):
    from tests.test_appointments_gateway import prepare
    from tests.test_owner_realtime import ready
    client,m,p,gateway,unused,r=prepare(service,tmp_path)
    m.transition(unused,'completed')
    m.settings.owner_calendar_coaching_enabled=True
    p.capabilities=replace(p.capabilities,owner_agenda=True)
    async def read(interval):
        event=CalendarEvent(EventIdentity(p.identity,'private'),'r',SLOT,owner_label='Only owner hears this')
        return Coverage(interval,NOW,NOW+timedelta(seconds=30),True,(event,))
    p.read_agenda=read
    client,m,g,cid,tid=ready(service)
    ch=m.observer.realtime.channels[tid]
    result=client.portal.call(ch.session.calendar_lookup,query())
    assert result['status']=='current' and result['private_owner_only']
    assert 'Only owner hears this' not in str(m.incoming_context(m.db.get(cid)))
    ch.facts_delivery_until=time.monotonic()+.001
    client.portal.call(ch.session.calendar_lookup,query())
    assert ch.delivery_until<=ch.facts_delivery_until
    ch.delivery_permission=(ch.generation,'snapshot')
    async def expired():
        await asyncio.sleep(.005)
        assert not m.observer.realtime.valid_frame(tid,ch,ch.generation,'snapshot')
    client.portal.call(expired)
    m.observer.paused.add(tid)
    assert client.portal.call(ch.session.calendar_lookup,query())['status']=='unavailable'
