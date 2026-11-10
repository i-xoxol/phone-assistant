"""Explicit opt-in PostgreSQL replay; never reads the developer's real .env."""
import hashlib
import os
from uuid import uuid4

import pytest


def pytest_addoption(parser):
    parser.addoption('--postgres-tests', action='store_true', help='Replay selected tests against TEST_DATABASE_URL')


@pytest.fixture(autouse=True)
def postgres_replay(request, monkeypatch):
    if not request.config.getoption('--postgres-tests'):
        yield
        return
    url = os.environ.get('TEST_DATABASE_URL', '')
    if not url:
        pytest.fail('--postgres-tests requires an isolated TEST_DATABASE_URL')
    import psycopg
    from psycopg import sql
    from app.storage.backend import close_pools
    from app.storage.database import Database
    from app.oauth_provider import PairingOAuthProvider
    from app.appointments.registry import Registry
    schemas = set()
    nonce = uuid4().hex
    for cls in (Database, PairingOAuthProvider, Registry):
        original = cls.__init__
        def init(self, path, *args, _original=original, _name=cls.__name__, **kwargs):
            key = nonce + request.node.nodeid + str(path) + _name
            schema = 'test_' + hashlib.sha256(key.encode()).hexdigest()[:28]
            schemas.add(schema)
            kwargs.update(database_url=url, schema=schema)
            _original(self, path, *args, **kwargs)
        monkeypatch.setattr(cls, '__init__', init)
    try:
        yield
    finally:
        close_pools()
        with psycopg.connect(url, autocommit=True) as connection:
            for schema in schemas:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
