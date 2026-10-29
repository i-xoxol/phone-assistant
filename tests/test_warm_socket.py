import asyncio
from types import SimpleNamespace

from app.calling.live_session import BridgeFailure, prepared_connection
from tests.test_calls import service
from tests.test_live import FakeClient, FakeConnection


def test_preconnect_is_reused_only_after_duration_cap(service, monkeypatch):
    _, manager, gateway = service
    async def run():
        connection = FakeConnection()
        closed = asyncio.Event()
        class Socket(FakeConnection):
            async def __aexit__(self, *args): closed.set()
        connection = Socket()
        monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(connection))
        calls = []
        async def cap(*args): calls.append(args)
        gateway.prepare_inbound = cap
        manager.warm_sockets.start('test')
        entry = manager.warm_sockets.entries['test']
        await asyncio.wait_for(entry.ready.wait(), 1)
        assert connection.config is None
        async with prepared_connection(manager.settings, manager, {
            'call_id':'test', 'direction':'inbound', 'twilio_call_sid':'CA_TEST', 'max_duration_minutes':5
        }) as actual:
            assert actual is connection and calls and connection.config is None
            assert not closed.is_set()
        await asyncio.wait_for(closed.wait(), 1)
        await manager.warm_sockets.stop()
    asyncio.run(run())


def test_unanswered_and_shutdown_close_unclaimed_socket(service, monkeypatch):
    _, manager, _ = service
    async def run():
        closed = asyncio.Event()
        class Socket(FakeConnection):
            async def __aexit__(self, *args): closed.set()
        monkeypatch.setattr('app.calling.live_session.live_client', lambda _: FakeClient(Socket()))
        manager.warm_sockets.start('test')
        manager.warm_sockets.start('test')
        entry = manager.warm_sockets.entries['test']
        await asyncio.wait_for(entry.ready.wait(), 1)
        manager.warm_sockets.ended('test')
        await asyncio.wait_for(closed.wait(), 1)
        assert await manager.warm_sockets.take('test') is None
        await manager.warm_sockets.stop()
        assert not manager.warm_sockets.tasks
    asyncio.run(run())


def test_failed_preconnect_falls_back_to_normal_connection(service, monkeypatch):
    _, manager, gateway = service
    async def run():
        attempts=[]
        class Failed(FakeConnection):
            async def __aenter__(self): raise OSError('unavailable')
        fresh=FakeConnection()
        def client(_):
            attempts.append(True)
            return FakeClient(Failed() if len(attempts)==1 else fresh)
        monkeypatch.setattr('app.calling.live_session.live_client',client)
        async def cap(*_): pass
        gateway.prepare_inbound=cap
        manager.warm_sockets.start('test')
        entry=manager.warm_sockets.entries['test']
        await asyncio.wait_for(entry.task,1)
        async with prepared_connection(manager.settings,manager,{
            'call_id':'test','direction':'inbound','twilio_call_sid':'CA_TEST','max_duration_minutes':5
        }) as connection:
            assert connection is fresh
        assert len(attempts)==2
        await manager.warm_sockets.stop()
    asyncio.run(run())


def test_cap_failure_never_starts_warmed_session(service, monkeypatch):
    _, manager, gateway = service
    async def run():
        connection=FakeConnection()
        monkeypatch.setattr('app.calling.live_session.live_client',lambda _:FakeClient(connection))
        async def cap(*_): raise RuntimeError('failed')
        gateway.prepare_inbound=cap
        manager.warm_sockets.start('test')
        entry=manager.warm_sockets.entries['test']
        await asyncio.wait_for(entry.ready.wait(),1)
        try:
            async with prepared_connection(manager.settings,manager,{
                'call_id':'test','direction':'inbound','twilio_call_sid':'CA_TEST','max_duration_minutes':5
            }):
                assert False
        except BridgeFailure as e:
            assert e.reason=='incoming_duration_limit_failed'
        assert connection.config is None
        await asyncio.wait_for(entry.task,1)
        await manager.warm_sockets.stop()
    asyncio.run(run())


def test_expired_preconnected_transport_retries_fresh(service, monkeypatch):
    from app.calling.live_session import started_connection
    _, manager, gateway = service
    async def run():
        cid=manager.db.create_inbound('CA_RETRY','+12025550101',5,10,'concierge')
        manager.transition(cid,'in_progress')
        class Expired(FakeConnection):
            async def start(self, session): raise OSError('transport closed')
        fresh=FakeConnection(); clients=[]
        def client(_):
            clients.append(True)
            return FakeClient(Expired() if len(clients)==1 else fresh)
        monkeypatch.setattr('app.calling.live_session.live_client',client)
        async def cap(*_): pass
        gateway.prepare_inbound=cap
        manager.warm_sockets.start(cid)
        await asyncio.wait_for(manager.warm_sockets.entries[cid].ready.wait(),1)
        async with started_connection(manager.settings,manager,manager.db.get(cid)) as (connection,record,started):
            assert connection is fresh and started['session']['id']=='live_test'
        assert len(clients)==2
        await manager.warm_sockets.stop()
    asyncio.run(run())
