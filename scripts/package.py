"""Build allowlisted source and private plugin archives; never include runtime data."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ('app', 'scripts', 'tests', 'deploy', 'docs', 'examples', 'plugin')
FILES = ('requirements.txt', 'requirements-dev.txt', 'README.md', '.env.example',
         '.gitignore', '.gitattributes', '.editorconfig', 'run.py', 'LICENSE', 'SECURITY.md', 'CONTRIBUTING.md', 'CHANGELOG.md')
ALLOWED_SUFFIXES = {'.py', '.ps1', '.md', '.json', '.html', '.css', '.js', '.svg', '.service'}


def source_files():
    for folder in FOLDERS:
        for path in sorted((ROOT/folder).rglob('*')):
            if (path.is_file() and '__pycache__' not in path.parts
                    and path.suffix in ALLOWED_SUFFIXES and not path.name.startswith('.env')):
                yield path
    for name in FILES:
        yield ROOT/name


def main():
    output = ROOT/'data'
    output.mkdir(exist_ok=True)
    with ZipFile(output/'phone-agent-service.zip', 'w', ZIP_DEFLATED) as archive:
        for path in source_files():
            archive.write(path, path.relative_to(ROOT))
    with ZipFile(output/'phone-assistant-plugin.zip', 'w', ZIP_DEFLATED) as archive:
        for path in sorted((ROOT/'plugin').rglob('*')):
            if path.is_file() and path.suffix in {'.md', '.json'}:
                archive.write(path, path.relative_to(ROOT/'plugin'))
    print('Created allowlisted archives in ignored data/. Review customized plugin endpoints before sharing.')


if __name__ == '__main__':
    main()
