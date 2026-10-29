"""Short-lived, per-call OpenAI socket preparation; never starts a session."""
import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from app.logging_config import log_event


@dataclass
class Entry:
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    connection: object = None
    task: object = None


class WarmSockets:
    def __init__(self, manager):
        self.manager = manager
        self.entries = {}
        self.tasks = set()

    def start(self, call_id):
        if call_id in self.entries or len(self.entries) >= 10:
            return
        entry = Entry()
        self.entries[call_id] = entry
        entry.task = asyncio.create_task(self._open(call_id, entry))
        self.tasks.add(entry.task)
        entry.task.add_done_callback(self.tasks.discard)

    async def _open(self, call_id, entry):
        from app.calling.live_session import live_client
        try:
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(5):
                    client = await stack.enter_async_context(live_client(self.manager.settings))
                    entry.connection = await stack.enter_async_context(client.live.connect())
                entry.ready.set()
                log_event('incoming_voice_socket_ready', call_id=call_id)
                claimed = asyncio.create_task(entry.claimed.wait())
                released = asyncio.create_task(entry.release.wait())
                try:
                    done, _ = await asyncio.wait([claimed, released], timeout=30, return_when=asyncio.FIRST_COMPLETED)
                    if claimed in done and not entry.release.is_set():
                        await entry.release.wait()
                finally:
                    claimed.cancel(); released.cancel()
                    await asyncio.gather(claimed, released, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event('incoming_voice_socket_unavailable', call_id=call_id, error_type=type(exc).__name__)
        finally:
            entry.connection = None
            entry.ready.set()
            if self.entries.get(call_id) is entry:
                self.entries.pop(call_id, None)

    async def take(self, call_id):
        entry = self.entries.get(call_id)
        if not entry or entry.claimed.is_set() or entry.release.is_set():
            return None
        try:
            await asyncio.wait_for(entry.ready.wait(), timeout=.5)
        except TimeoutError:
            self.ended(call_id)
            return None
        if not entry.connection or entry.claimed.is_set() or entry.release.is_set() or entry.task.done():
            return None
        entry.claimed.set()
        return entry

    def ended(self, call_id):
        entry = self.entries.get(call_id)
        if entry and not entry.claimed.is_set():
            entry.release.set()

    async def stop(self):
        for entry in list(self.entries.values()):
            entry.release.set()
            if entry.task and not entry.task.done():
                entry.task.cancel()
        tasks = list(self.tasks)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.difference_update(tasks)
