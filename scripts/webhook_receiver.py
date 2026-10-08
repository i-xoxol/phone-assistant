"""Optional example receiver: uvicorn scripts.webhook_receiver:app --port 8780.

Configure its WEBHOOK_SECRET to match the phone service. Expose this receiver
with a separate HTTPS tunnel, then set that /events URL on the phone service.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response

app=FastAPI()


@app.post('/events')
async def receive(request: Request):
    secret=os.environ.get('WEBHOOK_SECRET','')
    if not secret:
        raise HTTPException(503,'Receiver signing secret is not configured')
    body=await request.body()
    timestamp=request.headers.get('x-phone-timestamp','')
    try:
        if abs(time.time()-int(timestamp))>300:
            raise ValueError()
    except ValueError:
        raise HTTPException(401,'Stale or invalid timestamp') from None
    expected='sha256='+hmac.new(secret.encode(),timestamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,request.headers.get('x-phone-signature','')):
        raise HTTPException(401,'Invalid signature')
    event=json.loads(body)
    if str(event.get('id'))!=request.headers.get('x-phone-event-id'):
        raise HTTPException(400,'Event ID mismatch')
    path=Path('data/received-events.sqlite3');path.parent.mkdir(exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS received (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')
        db.execute('INSERT OR IGNORE INTO received VALUES (?,?)',(event['id'],body.decode()))
    path.chmod(0o600)
    return Response(status_code=204)
