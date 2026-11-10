"""Storage/configuration boundaries shared by SQLite and opt-in PG replay."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.storage.database import Database
from app.storage.models import CallRequest
from scripts.backup_state import backup
from tests.test_calls import service


def test_managed_requires_postgres_and_redacts_secrets():
    with pytest.raises(ValidationError, match='requires PostgreSQL'):
        Settings(_env_file=None, deployment_mode='managed')
    bad='mysql://owner:do-not-print@db.example.test/database'
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url=bad)
    assert 'do-not-print' not in str(error.value)
    assert 'do-not-print' not in repr(Settings(_env_file=None,
        database_url='postgresql://owner:do-not-print@db.example.test/database'))


def test_pg_never_takes_a_misleading_sqlite_backup(tmp_path):
    settings=Settings(_env_file=None,database_url='postgresql://owner:secret@db.example.test/database')
    with pytest.raises(ValueError,match='SQLite backup is disabled'):
        backup(settings,tmp_path/'backup')
    assert not (tmp_path/'backup').exists()


def test_empty_secret_keeps_sqlite_default(tmp_path):
    db=Database(tmp_path/'new'/'calls.db',database_url=SecretStr(''))
    # In PG replay the fixture explicitly chooses its isolated schema instead.
    with db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM calls').fetchone()[0]==0


def test_tool_claim_is_atomic_across_threads(tmp_path):
    db=Database(tmp_path/'calls.db')
    cid=db.create(CallRequest(phone_number='+12025550101',objective='Offline test'))
    with ThreadPoolExecutor(max_workers=4) as workers:
        results=list(workers.map(lambda _:db.claim_tool(cid,'stable-id','send_digits'), range(8)))
    assert results.count(True)==1
    assert results.count(False)==7
    assert Database(db.path).get(cid)['objective']=='Offline test'


def test_readiness_handles_database_failure_without_leaking(service,monkeypatch):
    client,manager,_=service
    assert client.get('/readyz').json()=={'ready':True}
    def unavailable():
        raise sqlite3.OperationalError('private database credential must stay hidden')
    monkeypatch.setattr(manager.db,'connect',unavailable)
    response=client.get('/readyz')
    assert response.status_code==503 and response.json()=={'ready':False}
    assert 'private' not in response.text


def test_sql_translation_preserves_values_and_case_end():
    pytest.importorskip('sqlparse')
    from app.storage.backend import _sql
    assert _sql("SELECT CASE WHEN active=? THEN end ELSE '100% ?' END FROM slots") == (
        'SELECT CASE WHEN active=%s THEN "end" ELSE \'100%% ?\' END FROM slots')
