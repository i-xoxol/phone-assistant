from pathlib import Path

import pytest

from scripts.audit_release import content_problems, path_problem


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
