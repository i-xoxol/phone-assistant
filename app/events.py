"""Event-driven wakeups with a durable SQLite log for replay. One uvicorn worker."""
import asyncio
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager, suppress

import httpx

from app.logging_config import log_event


class EventHub:
    def __init__(self):
        self.listeners = set()

    @asynccontextmanager
    async def subscribe(self):
        pair = (asyncio.get_running_loop(), asyncio.Event())
        self.listeners.add(pair)
        try:
            yield pair[1]
        finally:
            self.listeners.discard(pair)

    def notify(self):
        for loop, ready in tuple(self.listeners):
            if not loop.is_closed():
                loop.call_soon_threadsafe(ready.set)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    return 'sha256=' + hmac.new(secret.encode(), timestamp.encode() + b'.' + body, hashlib.sha256).hexdigest()


class WebhookDispatcher:
    """Ordered, at-least-once delivery. Slow receivers cannot block phone audio."""
    def __init__(self, manager):
        self.manager = manager
        self.url = manager.settings.webhook_url
        self.secret = manager.settings.webhook_secret.get_secret_value()
        self.destination = hashlib.sha256(self.url.encode()).hexdigest()
        self.task = None

    async def start(self):
        if not self.url:
            return
        if not self.secret:
            raise ValueError('WEBHOOK_SECRET is required when WEBHOOK_URL is configured')
        # First configuration starts at current head: no unsolicited export of old calls.
        self.manager.db.webhook_cursor(self.destination)
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task

    async def deliver(self, client, event):
        body = json.dumps(event, ensure_ascii=False, separators=(',', ':')).encode()
        stamp = str(int(time.time()))
        response = await client.post(self.url, content=body, headers={
            'Content-Type': 'application/json', 'X-Phone-Event-Id': str(event['id']),
            'X-Phone-Timestamp': stamp, 'X-Phone-Signature': signature(self.secret, stamp, body)})
        response.raise_for_status()
        self.manager.db.webhook_ack(self.destination, event['id'])

    async def run(self):
        failures = 0
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            async with self.manager.events.subscribe() as ready:
                while True:
                    ready.clear()
                    events = self.manager.db.events_after(self.manager.db.webhook_cursor(self.destination), limit=100)
                    if not events:
                        try:
                            await asyncio.wait_for(ready.wait(), 15)
                        except TimeoutError:
                            pass
                        continue
                    for event in events:
                        try:
                            await self.deliver(client, event)
                            failures = 0
                        except Exception as exc:
                            failures += 1
                            log_event('webhook_delivery_retry', event_id=event['id'], error_type=type(exc).__name__)
                            await asyncio.sleep(min(60, 2 ** min(failures, 6)))
                            break
