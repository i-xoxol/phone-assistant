"""LOCAL DEMO ONLY: reset its test password, verify HTTPS login and restart persistence."""
import argparse
import http.cookiejar
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import build_opener, HTTPCookieProcessor, HTTPSHandler, Request


def verify(sudo=False):
    root=Path(__file__).resolve().parents[1]
    compose=(['sudo','-n'] if sudo else [])+['docker','compose','-f',str(root/'deploy/managed/compose.yaml')]
    def command(*args):
        return subprocess.check_output([*compose,*args],text=True,cwd=root).strip()
    certificate=command('exec','-T','portal','cat','/data/caddy/pki/authorities/local/root.crt')
    context=ssl.create_default_context(cadata=certificate)
    jar=http.cookiejar.CookieJar()
    client=build_opener(HTTPSHandler(context=context),HTTPCookieProcessor(jar))
    url='https://localhost:18767'
    def request(path,body=None):
        data=urlencode(body).encode() if body is not None else None
        return client.open(Request(url+path,data=data,headers={'Origin':url}),timeout=30)
    # Reset ONLY the named local compose project's public demo credentials.
    code=command('exec','-T','phone','python','-m','scripts.web_pairing','--reset-password')
    assert request('/live/login',{'code':code}).status==200
    assert isinstance(json.load(request('/live/bootstrap')),dict)
    page=request('/live/password').read().decode()
    csrf=re.search(r'name="csrf" value="([^"]+)"',page)[1]
    password='synthetic-local-demo-password'
    assert request('/live/password',{'csrf':csrf,'new_password':password,'confirm_password':password}).status==200
    call=json.loads(subprocess.check_output([sys.executable,'-m','scripts.managed_smoke'],text=True,cwd=root))['call_id']
    command('exec','-T','phone','python','-c',
        "from pathlib import Path; p=Path('/data/owner-prompts/local-smoke.txt'); p.parent.mkdir(exist_ok=True); p.write_text('synthetic file persists')")
    command('restart','phone')
    for _ in range(30):
        try:
            if json.load(request('/readyz'))['ready']:break
        except Exception:time.sleep(1)
    else:raise AssertionError('Local demo restart readiness failed')
    jar.clear()
    assert request('/live/password/login',{'username':'owner','password':password}).status==200
    assert isinstance(json.load(request('/live/bootstrap')),dict)
    subprocess.run([sys.executable,'-m','scripts.managed_smoke','--call-id',call],check=True,cwd=root,capture_output=True)
    assert command('exec','-T','phone','python','-c',
        "from pathlib import Path; print(Path('/data/owner-prompts/local-smoke.txt').read_text())")=='synthetic file persists'
    print(json.dumps({'local_https_login':'passed','password_after_restart':'passed',
        'postgres_and_file_persistence':'passed','real_calls':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sudo',action='store_true',help='Use sudo -n for Docker on a Linux development host')
    verify(parser.parse_args().sudo)
