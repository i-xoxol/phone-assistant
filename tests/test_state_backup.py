from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest
from scripts.backup_state import backup


def settings(tmp_path):
    return SimpleNamespace(database_path=tmp_path/'calls.sqlite3', oauth_database_path=tmp_path/'oauth.sqlite3',
        appointment_database_path=tmp_path/'appointments.sqlite3', owner_prompt_directory=tmp_path/'prompts',
        recording_notice_directory=tmp_path/'notices', appointment_google_token_file=None)


def test_online_wal_backup_retains_committed_rows_and_private_inputs(tmp_path):
    value=settings(tmp_path)
    source=sqlite3.connect(value.database_path)
    source.execute('PRAGMA journal_mode=WAL')
    source.execute('CREATE TABLE messages(text TEXT)')
    source.execute("INSERT INTO messages VALUES('saved message')")
    source.commit()
    # Keep a writer connection open, so a raw file copy would omit the WAL row.
    secret=tmp_path/'source.env';secret.write_text('SYNTHETIC_SECRET=example')
    archive=backup(value,tmp_path/'backups',env_file=secret)
    with tarfile.open(archive) as handle:
        assert set(handle.getnames())=={'calls.sqlite3','phone.env'}
        assert handle.extractfile('phone.env').read()==secret.read_bytes()
        restore=tmp_path/'restore.sqlite3';restore.write_bytes(handle.extractfile('calls.sqlite3').read())
    with sqlite3.connect(restore) as restored:
        assert restored.execute('SELECT text FROM messages').fetchall()==[('saved message',)]
        assert restored.execute('PRAGMA quick_check').fetchone()[0]=='ok'
    source.close()


def test_backup_refuses_symlinked_secret(tmp_path):
    value=settings(tmp_path)
    with sqlite3.connect(value.database_path) as db:db.execute('CREATE TABLE calls(id TEXT)')
    target=tmp_path/'token';target.write_text('synthetic')
    link=tmp_path/'linked-token'
    try:link.symlink_to(target)
    except OSError:pytest.skip('Symlinks unavailable for this account')
    with pytest.raises(ValueError):backup(value,tmp_path/'backups',cli_auth=link)
    assert not list((tmp_path/'backups').glob('phone-state-*.tar.gz'))
