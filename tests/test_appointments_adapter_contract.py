"""Identical orchestration outcomes against unrelated provider identities and mocked Google I/O."""
import json
from dataclasses import replace
import httpx,pytest
from app.appointments.contracts import ProviderFault
from tests.appointment_fixtures import setup,FakeProvider,NOW
from tests.test_appointments_google import adapter,office,health

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.fixture(params=['fake','google'])
async def provider(request):
    if request.param=='fake':
        p=FakeProvider(lambda:NOW)
        yield p,lambda:setattr(p,'fail','after'),lambda:p.writes
    else:
        saved={};state={'lose':False,'writes':0}
        def handle(r):
            if r.url.host=='mirror.test':return httpx.Response(200,json=health())
            if r.method=='POST':
                state['writes']+=1;body=json.loads(r.content);saved[body['id']]={**body,'etag':'opaque-google-etag'}
                if state['lose']:raise httpx.ReadTimeout('synthetic lost response')
                return httpx.Response(200,json=saved[body['id']])
            eid=r.url.path.rsplit('/',1)[-1]
            if eid!='events':return httpx.Response(200,json=saved[eid]) if eid in saved else httpx.Response(404)
            if 'syncToken' in r.url.params:return httpx.Response(410)
            return httpx.Response(200,json={'items':[office(),*saved.values()],'nextSyncToken':'google-cursor'})
        p=adapter(handle)
        try:yield p,lambda:state.update(lose=True),lambda:state['writes']
        finally:await p.http.aclose()

@pytest.mark.anyio
@pytest.mark.parametrize('lost',[False,True])
async def test_shared_create_reconcile_registry_and_minimal_disclosure(tmp_path,provider,lost):
    s,db,unused,d,state,now=setup(tmp_path);p,lose,count=provider;s.provider=p
    if lost:lose()
    check=await s.check('call-1',d['draft_id']);assert check['decision']=='available' and not check['reservation']
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable-key')
    assert result['state']=='confirmed' and count()==1
    result2=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable-key')
    assert result2['operation_id']==result['operation_id'] and count()==1
    own=await s.own_appointment('Casey','Example')
    assert own['status']=='match' and own['duration_seconds']==900
    assert set(own)=={'status','start','end','zone','location','duration_seconds'}
    assert (await s.status('call-1',result['operation_id']))['state']=='confirmed'

@pytest.mark.anyio
async def test_shared_unsupported_privacy_prevents_write(tmp_path,provider):
    s,db,unused,d,state,now=setup(tmp_path);p,lose,count=provider;s.provider=p
    p.capabilities=replace(p.capabilities,private_events=False)
    check=await s.check('call-1',d['draft_id'])
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable-key')
    assert result['state']=='needs_owner' and count()==0

@pytest.mark.anyio
async def test_shared_pretransport_guard_blocks_hangup_during_provider_preparation(tmp_path,provider):
    s,db,unused,d,state,now=setup(tmp_path);p,lose,count=provider;s.provider=p
    check=await s.check('call-1',d['draft_id']);create=p.create
    async def prepared(identity,value,*,authorize):
        state['active']=False  # E.g. hangup during OAuth refresh, before provider POST.
        await create(identity,value,authorize=authorize)
    p.create=prepared
    result=await s.book('call-1',d['draft_id'],check['check_id'],'yes','stable-key')
    assert result['state']=='needs_owner' and result['step']=='write_not_started'
    assert not result['write_attempted'] and count()==0
