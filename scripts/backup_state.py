"""Private, consistent SQLite backups for a single-server deployment. Never prints state or secrets."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile

from app.config import Settings, ROOT


def private_copy(source: Path, destination: Path):
    if not source.is_file() or source.is_symlink():
        raise ValueError('Backup inputs must be regular files')
    shutil.copyfile(source, destination)
    destination.chmod(0o600)


def backup(settings, directory, *, env_file=None, cli_auth=None, extra_files=()):
    directory = Path(directory).absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or (os.name != 'nt' and directory.stat().st_mode & 0o077):
        raise ValueError('Use an owner-only, nonsymlink backup directory')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    destination = directory / ('phone-state-' + stamp + '.tar.gz')
    with tempfile.TemporaryDirectory(prefix='.phone-backup-', dir=directory) as temporary:
        stage = Path(temporary)
        for field, name in [('database_path','calls.sqlite3'), ('oauth_database_path','oauth.sqlite3'),
                            ('appointment_database_path','appointments.sqlite3')]:
            source = Path(getattr(settings, field)).absolute()
            if not source.is_file():
                if field == 'database_path':
                    raise ValueError('Call database is missing')
                continue
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=30)) as origin:
                with closing(sqlite3.connect(stage/name)) as target:
                    origin.backup(target)
                    if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('Backup integrity check failed')
                    target.commit()
                    target.execute('PRAGMA journal_mode=DELETE')
            (stage/name).chmod(0o600)
        for field in ('owner_prompt_directory', 'recording_notice_directory'):
            source = Path(getattr(settings, field))
            if source.exists():
                if source.is_symlink() or any(p.is_symlink() for p in source.rglob('*')):
                    raise ValueError('Backup media must not contain symlinks')
                shutil.copytree(source, stage/source.name)
        for source, name in [(env_file, 'phone.env'), (cli_auth, 'codex-auth.json'),
                             (settings.appointment_google_token_file, 'google-token.json')]:
            if source:
                private_copy(Path(source), stage/name)
        for source in extra_files:
            source=Path(source)
            if (stage/source.name).exists():
                raise ValueError('Duplicate backup filename')
            private_copy(source, stage/source.name)
        archive = stage/'complete.tar.gz'
        with tarfile.open(archive, 'w:gz') as handle:
            for path in sorted(stage.iterdir()):
                if path != archive:
                    handle.add(path, arcname=path.name)
        archive.chmod(0o600)
        os.replace(archive, destination)
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT/'.env')
    parser.add_argument('--cli-auth', type=Path, help='Explicitly include the existing CLI auth cache')
    parser.add_argument('--extra-file', type=Path, action='append', default=[], help='Explicit additional private dependency file')
    args = parser.parse_args()
    try:
        os.umask(0o077)
        path = backup(Settings(_env_file=args.env_file), args.directory,
                      env_file=args.env_file, cli_auth=args.cli_auth, extra_files=args.extra_file)
        print(json.dumps({'backup':str(path), 'bytes':path.stat().st_size, 'sqlite_checks':'passed'}))
    except Exception as error:
        raise SystemExit('Private backup failed ('+type(error).__name__+'); no secrets printed.') from None
