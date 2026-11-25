"""Provider acquisition fixtures are reconstructed with synthetic data."""

import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from applicant.boards.public import StructuredHTML
from applicant.domain.company_evidence import CompanyResponse

FIXTURES = Path(__file__).parent / 'fixtures' / 'sources'
_PRIVATE = re.compile(
    r'(?i)\b(?:[\w.+-]+@[a-z\d-]+\.[a-z\d.-]+|'
    r'(?:set-cookie|authorization|csrf(?:[-_]?token)?|cookie|access[-_]?token|'
    r'refresh[-_]?token|session(?:[-_]?id)?|nonce|account[-_]?id|user[-_]?id)'
    r'["\s]*[:=]|type\s*=\s*["\']hidden)'
)
_ACTIVE = re.compile(r'(?is)<script\b(?![^>]*\btype\s*=\s*["\']application/ld\+json["\'])')
_QUERY = re.compile(r'(?i)https?://[^\s<>"\']+[?&][^\s<>"\']+=')


def check_fixture(name, content):
    for reason, expression in (
        ('private metadata', _PRIVATE),
        ('active script', _ACTIVE),
        ('query parameter', _QUERY),
    ):
        if expression.search(content):
            raise AssertionError(f'{name}: {reason}')
    if name.endswith('.html'):
        parser = StructuredHTML()
        parser.feed(content)
        for script in parser.scripts:
            try:
                json.loads(script)
            except ValueError:
                raise AssertionError(f'{name}: malformed structured JSON') from None
    elif name.endswith('.json'):
        check_archived_responses(name, json.loads(content))


def check_archived_responses(name, value):
    if isinstance(value, list):
        for item in value:
            check_archived_responses(name, item)
    elif isinstance(value, dict):
        if 'body_base64' in value:
            try:
                body = CompanyResponse.model_validate(value).body.decode('utf-8')
            except (ValidationError, UnicodeError):
                raise AssertionError(f'{name}: invalid archived response') from None
            check_fixture(f'{name} response.html', body)
        else:
            for item in value.values():
                check_archived_responses(name, item)


def test_fixture_checker_decodes_archived_responses():
    response = CompanyResponse.capture('https://example.test/job', b'{"csrf":"synthetic-canary"}')
    with pytest.raises(AssertionError, match='private metadata') as error:
        check_fixture('archive.json', json.dumps({'responses': [response.model_dump()]}))
    assert 'synthetic-canary' not in str(error.value)


def test_committed_source_fixtures():
    for path in FIXTURES.rglob('*'):
        if path.is_file():
            check_fixture(path.name, path.read_text(encoding='utf-8'))


@pytest.mark.parametrize(
    'value',
    [
        '{"cookie": "synthetic-canary"}',
        'name,email\nexample,canary@fixture.invalid',
        '<script>window.state = "synthetic-canary"</script>',
        '<a href="https://example.test/path?account=synthetic-canary">Example</a>',
        '<input type="hidden" value="synthetic-canary">',
        '<script type="application/ld+json">invalid-json</script>',
    ],
)
def test_fixture_checker_detects_contamination_without_echo(value):
    with pytest.raises(AssertionError, match=r'^fixture\.html:') as error:
        check_fixture('fixture.html', value)
    assert 'synthetic-canary' not in str(error.value)
    assert 'fixture.invalid' not in str(error.value)


def test_fixture_checker_accepts_synthetic_structured_job():
    check_fixture(
        'fixture.html', '<script type="application/ld+json">{"@type":"JobPosting"}</script>'
    )
