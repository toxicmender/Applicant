"""Regression cases for partitioned Chromium state, with synthetic credentials only."""

import subprocess
import sys
from unittest.mock import patch

import httpx
import pytest

from applicant.cli import main
from applicant.infra.session_access import SessionAccess
from applicant.infra.session_store import SessionStore
from applicant.services.provider_setup import provider_session
from applicant.settings import Settings

COOKIE_VALUE = 'synthetic-cookie-canary'
UNKNOWN_FIELD = 'synthetic-field-canary'
PAGE_TEXT = 'synthetic-page-canary'
QUERY_VALUE = 'synthetic-query-canary'


def scoped_cookie():
    return {
        'name': 'account',
        'value': COOKIE_VALUE,
        'path': '/',
        'domain': 'in.indeed.com',
        'secure': True,
        'httpOnly': True,
        'sameSite': 'None',
        'expires': -1,
    }


def invalid_capture(kind, args):
    cookie = scoped_cookie()
    cookie.update(
        {
            'unknown': {UNKNOWN_FIELD: True},
            'partition': {'partitionKey': 'https://indeed.com/?q=' + QUERY_VALUE},
            'ancestor': {
                'partitionKey': 'https://indeed.com',
                '_crHasCrossSiteAncestor': PAGE_TEXT,
            },
        }[kind]
    )
    with (
        patch('sys.stdin.isatty', return_value=True),
        patch(
            'applicant.services.provider_setup._check_browser',
            return_value={'status': 'verified', 'state': {'cookies': [cookie], 'origins': []}},
        ),
    ):
        return main(args)


@pytest.mark.parametrize('replace', [False, True], ids=['initial', 'replacement'])
@pytest.mark.parametrize(
    'kind,detail',
    [
        ('unknown', '1 unsupported cookie field'),
        ('partition', 'invalid partitionKey'),
        ('ancestor', 'invalid _crHasCrossSiteAncestor'),
    ],
)
def test_invalid_cookie_fails_safely_before_install(tmp_path, kind, detail, replace):
    config = tmp_path / 'config.toml'
    config.write_text('store = "files"\nsession_storage = "plaintext"\n', encoding='utf-8')
    store = provider_session(
        Settings(data_dir=tmp_path, store='files', session_storage='plaintext'), 'indeed'
    )
    if replace:
        store.install(
            {},
            {'cookies': [scoped_cookie()], 'origins': []},
            'verified',
            metadata={'checked_at': '2026-11-01T00:00:00+00:00'},
        )
    before = store.path.read_bytes() if replace else None
    log = tmp_path / 'run.log'
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.test_partitioned_sessions',
            kind,
            '--config',
            str(config),
            '--data-dir',
            str(tmp_path),
            '--log-file',
            str(log),
            'setup',
            'provider',
            'indeed',
            *(['--reauth'] if replace else []),
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    # Compare booleans so a failure cannot echo credential bytes or a complete log.
    outputs = (proc.stdout, proc.stderr, log.read_text(encoding='utf-8'))
    private = any(
        secret in output
        for output in outputs
        for secret in (COOKIE_VALUE, UNKNOWN_FIELD, PAGE_TEXT, QUERY_VALUE)
    )
    assert not private, 'indeed capture validation: diagnostic exposed synthetic private data'
    assert proc.returncode == 2, 'indeed capture validation: expected configuration exit 2'
    expected = 'indeed capture validation: ' + detail
    reported = expected in proc.stderr and expected in outputs[2]
    assert reported, (
        f'indeed capture validation: expected safe diagnostic "{detail}" in stderr and log'
    )
    unchanged = store.path.read_bytes() == before if replace else not store.path.exists()
    assert unchanged, (
        'indeed capture validation: rejection changed the installed credential or timestamp'
    )


def test_http_rotation_preserves_same_name_partitioned_cookie(tmp_path):
    path = tmp_path / 'storage_state.json'
    store = SessionStore(path, 'indeed', mode='plaintext')
    ordinary = scoped_cookie()
    partitioned = {
        **ordinary,
        'value': 'partition-only',
        'partitionKey': 'https://indeed.com',
        '_crHasCrossSiteAncestor': True,
    }
    state = {'cookies': [ordinary, partitioned], 'origins': []}
    record = store.install({}, state, 'verified')
    access = SessionAccess(store, record, state)
    url = 'https://in.indeed.com/jobs'
    for expected, update in (
        (COOKIE_VALUE, 'account=rotated; Secure; Path=/'),
        ('rotated', 'account=; Secure; Path=/; Max-Age=0'),
    ):
        seen = []

        def response(request, seen=seen, update=update):
            seen.append(request.headers.get('cookie'))
            return httpx.Response(200, headers={'Set-Cookie': update})

        with httpx.Client(transport=httpx.MockTransport(response)) as http:
            access.http(http, lambda: http.get(url), accepted=lambda r: r.is_success)
        correct_header = seen == ['account=' + expected]
        assert correct_header, (
            'indeed HTTP exchange: only the ordinary cookie may enter the request'
        )
        reopened = SessionStore(path, 'indeed', mode='plaintext').active()
        assert reopened is not None, 'indeed HTTP rotation: session disappeared'
        cookies = reopened['cookies']
        preserved = [c for c in cookies if 'partitionKey' in c] == [partitioned]
        assert preserved, 'indeed HTTP rotation: browser-only cookie or partition metadata changed'
    assert len(cookies) == 1, 'indeed HTTP deletion: ordinary cookie survived Max-Age=0'


@pytest.mark.parametrize(
    'update',
    [
        'isolated=synthetic; Secure; Path=/; Partitioned',
        'account=replaced; Secure; Path=/; pArTiTiOnEd',
        'account=; Secure; Path=/; Partitioned; Max-Age=0',
    ],
)
def test_http_ignores_partitioned_redirect_updates(tmp_path, update):
    store = SessionStore(tmp_path / 'state.json', 'indeed', mode='plaintext')
    ordinary = scoped_cookie()
    partitioned = {
        **ordinary,
        'value': 'browser-only',
        'partitionKey': 'https://indeed.com',
        '_crHasCrossSiteAncestor': True,
    }
    state = {'cookies': [ordinary, partitioned], 'origins': []}
    record = store.install({}, state, 'verified')
    access = SessionAccess(store, record, state)
    seen = []

    def respond(request):
        seen.append(request.headers.get('cookie', ''))
        if request.url.path == '/jobs':
            return httpx.Response(
                302,
                headers=[
                    ('Location', '/results'),
                    ('Set-Cookie', update),
                    ('Set-Cookie', 'other=ordinary; Secure; Path=/'),
                ],
            )
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
        access.http(
            client,
            lambda: client.get('https://in.indeed.com/jobs'),
            accepted=lambda response: response.is_success,
        )
    assert len(seen) == 2
    assert set(seen[1].split('; ')) == {'account=' + COOKIE_VALUE, 'other=ordinary'}, (
        'indeed redirect: a partitioned response modified the outgoing ordinary cookies'
    )
    reopened = store.active()
    assert reopened is not None
    assert partitioned in reopened['cookies'] and ordinary in reopened['cookies'], (
        'indeed persistence: partitioned response modified a saved account cookie'
    )
    assert {cookie['name'] for cookie in reopened['cookies']} == {'account', 'other'}


if __name__ == '__main__':
    raise SystemExit(invalid_capture(sys.argv[1], sys.argv[2:]))
