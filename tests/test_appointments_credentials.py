import json
from datetime import timedelta
import httpx,pytest
from app.appointments.google import OwnerFileToken,SCOPE
from app.appointments.contracts import ProviderFault
from tests.appointment_fixtures import NOW

@pytest.fixture
def anyio_backend():return 'asyncio'

@pytest.mark.anyio
async def test_existing_token_refresh_is_cached_in_memory_and_never_changes_file(tmp_path):
    path=tmp_path/'owner-token.json'
    path.write_text(json.dumps({'scopes':[SCOPE],'expiry':(NOW-timedelta(minutes=1)).isoformat(),
        'client_id':'synthetic-client','client_secret':'synthetic-client-secret','refresh_token':'synthetic-refresh'}))
    path.chmod(0o600);original=path.read_bytes();seen=[]
    def handle(r):
        seen.append(r);assert r.url.host=='oauth2.googleapis.com'
        return httpx.Response(200,json={'access_token':'synthetic-access','expires_in':3600})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        token=OwnerFileToken(path,http,lambda:NOW)
        assert await token()=='synthetic-access' and await token()=='synthetic-access'
    assert len(seen)==1 and path.read_bytes()==original

@pytest.mark.anyio
@pytest.mark.parametrize('case',['scope','malformed','missing'])
async def test_unusable_credentials_fail_closed_without_network_or_raw_content(tmp_path,case):
    path=tmp_path/'owner-token.json'
    if case!='missing':
        path.write_text('{not a token' if case=='malformed' else json.dumps({'scopes':['wrong-scope']}));path.chmod(0o600)
    def forbidden(r):raise AssertionError('No provider call should be attempted')
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(ProviderFault,match='^unauthorized$'):await OwnerFileToken(path,http,lambda:NOW)()
