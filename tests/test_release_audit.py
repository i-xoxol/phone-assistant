from pathlib import Path

import pytest

from scripts.audit_release import content_problems, path_problem, reviewed_doc_image


@pytest.mark.parametrize('name', ['.env', 'data/calls.sqlite3', 'data/oauth.sqlite3',
                                'docs/recording.wav', 'app/private.key'])
def test_runtime_files_cannot_be_published(name):
    assert path_problem(Path(name))


def test_public_examples_are_safe_and_filled_config_is_detected():
    assert path_problem(Path('.env.example')) is None
    assert content_problems(Path('examples/call.json'), '{"phone_number":"+12025550101"}') == []
    assert content_problems(Path('.env.example'), 'OPENAI_API_KEY=not-empty')


def test_credential_scan_reports_location_without_value():
    # Construct a synthetic signature rather than store a credential-like literal.
    value = 'sk-' + 'x' * 30
    findings = content_problems(Path('app/example.py'), '\n' + value)
    assert findings == [('OpenAI API key', 2)]
    assert value not in str(findings)


def test_plugin_template_cannot_embed_operator_endpoint():
    text = '{"mcpServers":{"phone":{"url":"https://operator.example.test/mcp"}}}'
    assert content_problems(Path('plugin/.mcp.json'), text) == [('non-template plugin endpoint', 1)]


def test_binary_documentation_images_require_review_of_the_exact_bytes(tmp_path):
    import hashlib
    import json

    relative = Path('docs/assets/demo.jpg')
    image = tmp_path/relative
    image.parent.mkdir(parents=True)
    data = b'\xff\xd8\xff' + b'fictional test image' + b'\xff\xd9'
    image.write_bytes(data)
    assert not reviewed_doc_image(relative, tmp_path)
    manifest = tmp_path/'docs/assets/media-review.json'
    manifest.write_text(json.dumps({'images': {'demo.jpg': {
        'sha256': hashlib.sha256(data).hexdigest(),
        'privacy_reviewed': True, 'source': 'synthetic fixture',
    }}}))
    assert reviewed_doc_image(relative, tmp_path)
    image.write_bytes(data + b'changed')
    assert not reviewed_doc_image(relative, tmp_path)
    assert not reviewed_doc_image(Path('app/demo.jpg'), tmp_path)


def test_documentation_review_cannot_approve_an_arbitrary_binary_file(tmp_path):
    import hashlib
    import json

    relative = Path('docs/assets/not-an-image.jpg')
    binary = tmp_path/relative
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'not a JPEG')
    (binary.parent/'media-review.json').write_text(json.dumps({'images': {
        binary.name: {'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                      'privacy_reviewed': True, 'source': 'test'},
    }}))
    assert not reviewed_doc_image(relative, tmp_path)
