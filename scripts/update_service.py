"""Linux service update with a SQLite backup and no restart during an active call.

Uses the example service name and default data paths. Prevent new calls during maintenance.
"""
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

ROOT=Path(__file__).resolve().parents[1]

def command(*args):
    subprocess.run(args,cwd=ROOT,check=True)

if __name__=='__main__':
    if sys.platform=='win32':
        raise SystemExit('Run this on the service host, not the Windows development host.')
    dbpath=ROOT/'data/calls.sqlite3'
    with sqlite3.connect(dbpath) as db:
        active=db.execute("SELECT count(*) FROM calls WHERE status NOT IN ('completed','failed','busy','no_answer','cancelled')").fetchone()[0]
    if active:
        raise SystemExit('Active calls exist; update deferred.')
    with ZipFile(ROOT/'data/phone-agent-service.zip') as archive:
        for name in archive.namelist():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or path.parts[0] not in ('app','scripts','tests','deploy','docs','examples','plugin','requirements.txt','requirements-dev.txt','README.md','.env.example','.gitignore','.gitattributes','.editorconfig','run.py','LICENSE','SECURITY.md','CONTRIBUTING.md','CHANGELOG.md'):
                raise SystemExit('Unexpected archive member; update stopped.')
        command('systemctl','--user','stop','phone-agent.service')
        stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
        backup=ROOT/'data'/('backup-'+stamp)
        backup.mkdir(mode=0o700)
        for name in ('calls.sqlite3','oauth.sqlite3'):
            if (ROOT/'data'/name).exists():
                with sqlite3.connect(ROOT/'data'/name) as source, sqlite3.connect(backup/name) as target:
                    source.backup(target)
                (backup/name).chmod(0o600)
        with ZipFile(backup/'source.zip','w') as old:
            for folder in ('app','scripts','tests','deploy','docs','examples','plugin'):
                for path in (ROOT/folder).rglob('*'):
                    if path.is_file() and '__pycache__' not in path.parts:
                        old.write(path,path.relative_to(ROOT))
            for name in ('requirements.txt','requirements-dev.txt','README.md','.env.example','.gitignore','.gitattributes','.editorconfig','run.py','LICENSE','SECURITY.md','CONTRIBUTING.md','CHANGELOG.md'):
                old.write(ROOT/name, name)
        archive.extractall(ROOT)
    try:
        command(str(ROOT/'.venv/bin/python'),'-m','pip','install','-r','requirements-dev.txt')
        command(str(ROOT/'.venv/bin/python'),'-m','pytest','-q')
    except BaseException:
        with ZipFile(backup/'source.zip') as old:
            old.extractall(ROOT)
        command(str(ROOT/'.venv/bin/python'),'-m','pip','install','-r','requirements.txt')
        raise
    finally:
        command('systemctl','--user','start','phone-agent.service')
    print('Source updated, tests passed, backup saved; service started.')
