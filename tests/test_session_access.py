from http.cookiejar import CookieJar
from pathlib import Path

import httpx

from applicant.infra.session_access import (
    SessionExpired,
    active_access,
    apply_cookie_scope,
    run_with_session,
    seed_cookie_jar,
)
from applicant.infra.session_store import SessionStore


def test_cookie_jar_keeps_session_cookies_and_host_only_scope():
    jar = CookieJar()
    base = {
        'name': 'host',
        'value': 'synthetic',
        'domain': 'in.indeed.com',
        'path': '/jobs',
        'expires': -1,
        'secure': True,
        'httpOnly': True,
    }
    seed_cookie_jar(
        jar, {'cookies': [base, dict(base, name='expired', expires=130)]}, clock=lambda: 100
    )
    with httpx.Client(cookies=jar) as client:

        def header(url):
            request = client.build_request('GET', url)
            apply_cookie_scope(request, jar)
            return request.headers.get('cookie')

        assert header('https://in.indeed.com/jobs/search') == 'host=synthetic'
        assert not header('https://other.in.indeed.com/jobs/search')
        assert not header('http://in.indeed.com/jobs/search')


def test_expiry_restarts_with_a_new_anonymous_attempt(tmp_path: Path):
    store = SessionStore(tmp_path / 'storage_state.json', 'indeed', mode='plaintext')
    state = {
        'cookies': [{'name': 'test', 'value': 'synthetic', 'domain': '.indeed.com', 'path': '/'}],
        'origins': [],
    }
    store.install(store.snapshot(), state, 'verified')
    visited = []

    def search():
        authenticated = active_access.get() is not None
        visited.append((authenticated, 1))
        if authenticated:
            visited.append((authenticated, 2))
            raise SessionExpired('expired')
        return ['public page one']

    rows, outcome = run_with_session(store, search, public=True)
    assert visited == [(True, 1), (True, 2), (False, 1)]
    assert rows == ['public page one']
    assert outcome['access_mode'] == 'public'
    assert outcome['reason'] == 'session_expired'
    assert store.snapshot()['status'] == 'needs_setup'
