"""Example Standard Webhooks receiver for native or ordinary MCP subscriptions.

Run behind your own HTTPS tunnel with MCP_WEBHOOK_SECRET=whsec_<base64>.
Stores events without interpreting their text or invoking any tools.
"""
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from standardwebhooks.webhooks import Webhook, WebhookVerificationError

from app.mcp.callback import MAX_BYTES, validate_secret

app = FastAPI(title='Phone MCP webhook receiver')


@app.post('/events')
async def receive(request: Request):
    secret = os.environ.get('MCP_WEBHOOK_SECRET', '')
    try:
        validate_secret(secret)
    except ValueError:
        raise HTTPException(503, 'Receiver signing secret is not configured') from None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, 'Event too large')
        chunks.append(chunk)
    body = b''.join(chunks)
    try:
        value = Webhook(secret).verify(body, dict(request.headers))
    except (WebhookVerificationError, ValueError, TypeError):
        raise HTTPException(401, 'Invalid event signature') from None
    if not isinstance(value, dict):
        raise HTTPException(400, 'Invalid event')
    if value.get('type') == 'verification':
        challenge = value.get('challenge')
        if not isinstance(challenge, str) or not 1 <= len(challenge) <= 128:
            raise HTTPException(400, 'Invalid verification challenge')
        return {'challenge': challenge}
    event_id = value.get('eventId')
    if (not isinstance(event_id, str) or request.headers.get('webhook-id') != event_id
            or value.get('name') not in ('transcript.delta', 'call.status')):
        raise HTTPException(400, 'Invalid application event')
    path = Path('data/received-mcp-events.sqlite3')
    path.parent.mkdir(exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS received (event_id TEXT PRIMARY KEY, body BLOB NOT NULL)')
        db.execute('INSERT OR IGNORE INTO received VALUES (?,?)', (event_id, body))
    if os.name != 'nt':
        path.chmod(0o600)
    return Response(status_code=204)
