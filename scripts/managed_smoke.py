"""Exercise the local managed demo with synthetic data; never calls Twilio."""
import argparse
import json
from urllib.request import Request, urlopen


def smoke(url, token, call_id=None):
    def request(path, body=None):
        headers={'Authorization':'Bearer '+token}
        data=None
        if body is not None:
            data=json.dumps(body).encode()
            headers['Content-Type']='application/json'
        with urlopen(Request(url.rstrip('/')+path, data=data, headers=headers), timeout=30) as response:
            return json.load(response)
    assert request('/readyz')['ready'] is True
    if call_id is None:
        result=request('/calls', {'phone_number':'+12025550101', 'objective':'Local managed deployment demonstration'})
        call_id=result['call_id']
        assert request('/demo/complete/'+call_id, {})['simulated'] is True
    result=request('/calls/'+call_id+'/result')
    assert result['status']=='completed'
    assert len(result['transcript'])==2
    print(json.dumps({'local_demo':'passed', 'call_id':call_id, 'real_calls':0,
        'persistence_recheck':'Run again with --call-id after restarting the phone container'}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url',default='http://127.0.0.1:18766')
    parser.add_argument('--token',default='local-demo-only',help='Local demo token only; do not use production credentials')
    parser.add_argument('--call-id',help='Recheck an existing synthetic call after a container restart')
    args=parser.parse_args()
    smoke(args.url,args.token,args.call_id)
