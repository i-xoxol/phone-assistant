"""Audit public source paths and credential patterns; prints locations only.

A release gate, not a substitute for human review or GitHub secret scanning.
It reads the repository index, never private runtime directories.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {'app', 'scripts', 'tests', 'deploy', 'plugin', 'docs', 'examples', '.github'}
ROOT_FILES = {'README.md', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md',
              '.gitignore', '.gitattributes', '.editorconfig', '.env.example', 'requirements.txt', 'requirements-dev.txt', 'run.py'}
FORBIDDEN_PARTS = {'data', '.venv', '__pycache__', '.pytest_cache', 'node_modules', '.git',
                   '.codex-remote-attachments', 'backups'}
FORBIDDEN_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.log', '.zip', '.wav', '.mp3', '.m4a',
                      '.pem', '.key', '.p12', '.pfx', '.pyc'}
PATTERNS = {
    'OpenAI API key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}'),
    'GitHub credential': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})'),
    'Twilio identifier': re.compile(r'\b(?:AC|SK|CA|MZ)[0-9a-fA-F]{32}\b'),
    'Private key': re.compile(r'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
}
PHONE = re.compile(r'\+[1-9][0-9]{7,14}(?![0-9])')
FICTIONAL_PHONE = re.compile(r'\+' + '120255501' + r'[0-9]{2}$')
SECRET_FIELDS = {'OPENAI_API_KEY', 'TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN',
                 'TWILIO_PHONE_NUMBER', 'PUBLIC_BASE_URL', 'LOCAL_API_TOKEN',
                 'CALLBACK_NUMBER', 'WEBHOOK_URL', 'WEBHOOK_SECRET'}


def path_problem(path: Path) -> str | None:
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return 'runtime/private directory'
    if path.name.startswith('.env') and path.name != '.env.example':
        return 'environment file'
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return 'runtime/credential/media file'
    if len(path.parts) == 1:
        return None if path.name in ROOT_FILES else 'unexpected root file'
    return None if path.parts[0] in SOURCE_DIRS else 'unexpected source directory'


def content_problems(path: Path, text: str) -> list[tuple[str, int]]:
    found = []
    for label, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            found.append((label, text.count('\n', 0, match.start()) + 1))
    for match in PHONE.finditer(text):
        if not FICTIONAL_PHONE.fullmatch(match.group()):
            found.append(('non-example phone number', text.count('\n', 0, match.start()) + 1))
    if path.name == '.env.example':
        for number, line in enumerate(text.splitlines(), 1):
            key, sep, value = line.partition('=')
            if sep and key.strip() in SECRET_FIELDS and value.strip():
                found.append(('filled example configuration', number))
    if path.as_posix() == 'plugin/.mcp.json':
        import json
        try:
            servers = json.loads(text)['mcpServers']
            if any(v.get('url') != 'https://phone.example.com/mcp' for v in servers.values()):
                found.append(('non-template plugin endpoint', 1))
        except (ValueError, KeyError, TypeError):
            found.append(('invalid plugin template', 1))
    return found


def source_paths(root: Path = ROOT) -> list[Path]:
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root,
                             capture_output=True, text=True, check=True).stdout.strip()
        if Path(top).resolve() == root.resolve():
            indexed = subprocess.run(['git', 'ls-files', '-z'], cwd=root,
                                     capture_output=True, check=True).stdout
            return [Path(p.decode('utf-8')) for p in indexed.split(b'\0') if p]
    except (OSError, subprocess.CalledProcessError):
        pass
    paths = [Path(name) for name in ROOT_FILES if (root/name).is_file()]
    for folder in SOURCE_DIRS:
        for path in (root/folder).rglob('*'):
            relative = path.relative_to(root)
            if path.is_file() and not any(p in FORBIDDEN_PARTS for p in relative.parts):
                paths.append(relative)
    return sorted(paths)


def main() -> int:
    issues = []
    paths = source_paths()
    for path in paths:
        if problem := path_problem(path):
            issues.append(f'{path.as_posix()}: {problem}')
            continue
        absolute = ROOT/path
        if not absolute.is_file():
            issues.append(f'{path.as_posix()}: indexed file missing from working tree')
            continue
        try:
            text = absolute.read_text(encoding='utf-8')
        except (UnicodeError, OSError):
            issues.append(f'{path.as_posix()}: non-text source file needs review')
            continue
        issues.extend(f'{path.as_posix()}:{line}: {label}' for label, line in content_problems(path, text))
    if issues:
        print('Release audit failed (locations only; matched values are hidden):')
        print('\n'.join(issues))
        return 1
    print(f'Release audit passed: {len(paths)} source files; no detected credential or runtime-file issues.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
