import json
from datetime import datetime,timedelta,timezone

import httpx
import pytest

from scripts.sync_google_availability import access_token,busy_windows,free_windows,healthy_upstream

UTC=timezone.utc


def stamp(text): return datetime.fromisoformat(text)


def test_working_hours_busy_union_and_weekend_are_respected():
    current=stamp('2026-10-19T12:00:00+00:00')
    busy=[(stamp('2026-10-19T14:00:00+00:00'),stamp('2026-10-19T15:00:00+00:00')),
          (stamp('2026-10-19T14:30:00+00:00'),stamp('2026-10-19T16:00:00+00:00'))]
    result=free_windows(busy,current,days=1)
    assert result==[{'start':'2026-10-19T13:00:00+00:00','end':'2026-10-19T14:00:00+00:00'},
                    {'start':'2026-10-19T16:00:00+00:00','end':'2026-10-19T21:00:00+00:00'}]
    assert free_windows([],stamp('2026-10-17T12:00:00+00:00'),days=1)==[]


def test_dst_all_day_transparency_and_pagination_read_no_titles():
    requests=[]
    def respond(request):
        requests.append(request)
        assert request.method=='GET' and request.headers['Authorization']=='Bearer synthetic'
        fields=request.url.params['fields']
        assert 'summary' not in fields and 'description' not in fields and 'attendees' not in fields
        if request.url.params.get('pageToken'):
            return httpx.Response(200,json={'timeZone':'America/New_York','items':[{
                'start':{'date':'2026-11-02'},'end':{'date':'2026-11-03'}}]})
        return httpx.Response(200,json={'timeZone':'America/New_York','nextPageToken':'next','items':[
            {'transparency':'transparent','start':{'date':'2026-11-01'},'end':{'date':'2026-11-02'}},
            {'status':'cancelled'}, {'eventType':'workingLocation'}]})
    current=stamp('2026-10-30T12:00:00+00:00')
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        busy=busy_windows(client,'synthetic',['primary'],current,current+timedelta(days=5))
    assert len(requests)==2 and busy==[(stamp('2026-11-02T05:00:00+00:00'),stamp('2026-11-03T05:00:00+00:00'))]
    result=free_windows(busy,current,days=5)
    assert any(r['start']=='2026-11-03T14:00:00+00:00' for r in result)
    assert not any(r['start'].startswith('2026-11-02') for r in result)


def test_token_refresh_is_in_memory_only_and_fixed_google_origin(tmp_path):
    path=tmp_path/'token.json'
    saved={'expiry':'2020-01-01T00:00:00Z','client_id':'synthetic','client_secret':'synthetic',
        'refresh_token':'synthetic','token_uri':'https://untrusted.example.test/steal'}
    path.write_text(json.dumps(saved));path.chmod(0o600)
    original=path.read_bytes()
    def respond(request):
        assert str(request.url)=='https://oauth2.googleapis.com/token'
        assert request.method=='POST'
        return httpx.Response(200,json={'access_token':'refreshed-synthetic'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert access_token(path,client,datetime.now(UTC))=='refreshed-synthetic'
    assert path.read_bytes()==original


def test_stale_work_mirror_stops_refresh():
    now=datetime.now(UTC)
    for status in ({'state':'failed'},{'state':'healthy','lastSuccess':{'timestamp':now.timestamp()-1000}}):
        with httpx.Client(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=status))) as client:
            with pytest.raises(ValueError,match='stale'): healthy_upstream(client,'http://localhost/status',now)
