"""Test a source release in an isolated the service host environment before stopping service."""
import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    if sys.platform == 'win32':
        raise SystemExit('Run on the service host.')
    with tempfile.TemporaryDirectory(prefix='event-release-', dir=ROOT/'data') as folder:
        target = Path(folder)
        with ZipFile(ROOT/'data/phone-agent-service.zip') as archive:
            for name in archive.namelist():
                path = Path(name)
                if path.is_absolute() or '..' in path.parts:
                    raise SystemExit('Unexpected archive path')
            archive.extractall(target)
        def run(*args): subprocess.run(args, cwd=target, check=True)
        run(sys.executable, '-m', 'venv', str(target/'.venv'))
        python = str(target/'.venv/bin/python')
        run(python, '-m', 'pip', 'install', '-q', '-r', 'requirements-dev.txt')
        run(python, '-m', 'pytest', '-q')
        print('Isolated release tests passed; production service unchanged.')
