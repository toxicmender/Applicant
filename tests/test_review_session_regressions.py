import json
import multiprocessing

import httpx
import pytest

from applicant.errors import NotFound
from applicant.infra.session_access import run_with_session
from applicant.infra.session_store import SessionStore
from applicant.reviews.ambitionbox import AmbitionBoxClient
from applicant.services.company_cache import CompanyCache
from applicant.services.reviews import fetch_reviews

STATE = {
    'cookies': [
        {'name': 'account', 'value': 'synthetic', 'domain': '.ambitionbox.com', 'path': '/'}
    ],
    'origins': [],
}


def props(title):
    return {
        'companyName': 'Synthetic',
        'companyId': '1',
        'reviewCount': 2,
        'reviewsData': [{'reviewTitle': title}],
        'pagination': {'totalPages': 2},
    }


@pytest.mark.parametrize('public_fails', [False, True])
def test_ambitionbox_page_two_expiry_restarts_from_public_page_one(
    tmp_path, monkeypatch, public_fails
):
    store = SessionStore(tmp_path / 'storage_state.json', 'ambitionbox', mode='plaintext')
    installed = store.install({}, STATE, 'verified')
    assert installed is not None
    requests = []
    recorded = []
    output = tmp_path / 'ratings.json'
    output.write_text('{"old": true}', encoding='utf-8')
    cache = CompanyCache(output)
    cache.path.write_text('{}', encoding='utf-8')

    def response(request):
        private = request.headers.get('cookie') == 'account=synthetic'
        path = request.url.path
        requests.append((private, path))
        if path == '/login':
            return httpx.Response(200, text='Sign in')
        if 'private-A' in path:
            return httpx.Response(302, headers={'location': '/login'})
        if path.startswith('/_next/'):
            return httpx.Response(200, json={'pageProps': props('public two')})
        if not private and public_fails:
            return httpx.Response(404)
        build = 'private-A' if private else 'public-B'
        body = json.dumps({'buildId': build, 'props': {'pageProps': props(build)}})
        return httpx.Response(200, text=f'<script id="__NEXT_DATA__">{body}</script>')

    def attempt():
        staged = []
        with httpx.Client(transport=httpx.MockTransport(response), follow_redirects=True) as http:
            with AmbitionBoxClient(
                client=http, delay=0, capture=lambda *row: staged.append(row)
            ) as adapter:
                return adapter.fetch('synthetic', 2), staged

    client = AmbitionBoxClient()
    client.session_store = store

    def fetch(company, max_reviews):
        assert (company, max_reviews) == ('synthetic', 2)
        result, client.access = run_with_session(store, attempt, public=True)
        rating, evidence = result
        recorded.extend(evidence)
        return rating

    monkeypatch.setattr(client, 'fetch', fetch)
    try:
        if public_fails:
            with pytest.raises(NotFound):
                fetch_reviews('synthetic', {'ambitionbox': client}, str(output), max_reviews=2)
        else:
            found = fetch_reviews('synthetic', {'ambitionbox': client}, str(output), max_reviews=2)
    finally:
        client.close()
    assert requests[:4] == [
        (True, '/reviews/synthetic-reviews'),
        (True, '/_next/data/private-A/reviews/synthetic-reviews.json'),
        (True, '/login'),
        (False, '/reviews/synthetic-reviews'),
    ]
    assert store.snapshot()['generation'] == installed['generation']
    assert store.snapshot()['status'] == 'needs_setup'
    if public_fails:
        assert output.read_text(encoding='utf-8') == '{"old": true}'
        assert cache.path.read_text(encoding='utf-8') == '{}'
        assert not recorded
    else:
        assert found is not None
        assert requests[4:] == [(False, '/_next/data/public-B/reviews/synthetic-reviews.json')]
        assert [row.title for row in found.ratings[0].reviews] == ['public-B', 'public two']
        entry = cache.entry('ambitionbox', 'synthetic', 'public:default')
        assert entry is not None
        assert [row['title'] for row in entry['payload']['reviews']] == ['public-B', 'public two']
        assert all(b'private-A' not in body for _, body, _ in recorded)


def replace_profile(path, clear, ready, changed):
    store = SessionStore(path, 'ambitionbox', mode='plaintext')
    if not ready.wait(20):
        raise RuntimeError('review never reached the publication boundary')
    if clear:
        store.clear()
    else:
        assert store.install(store.snapshot(), STATE, 'verified') is not None
    changed.set()


@pytest.mark.parametrize('transition', ['public_reauth', 'private_reauth', 'private_clear'])
def test_review_cache_uses_completed_attempt_when_profile_changes(
    tmp_path, monkeypatch, transition
):
    from applicant.infra.session_access import SessionExpired
    from applicant.reviews.models import CompanyRating

    store = SessionStore(tmp_path / 'storage_state.json', 'ambitionbox', mode='plaintext')
    original = store.install({}, STATE, 'verified')
    assert original is not None
    output = tmp_path / 'ratings.json'
    public = transition == 'public_reauth'
    clear = transition == 'private_clear'
    ctx = multiprocessing.get_context('spawn')
    ready, changed = ctx.Event(), ctx.Event()
    child = ctx.Process(target=replace_profile, args=(store.path, clear, ready, changed))
    client = AmbitionBoxClient()
    client.session_store = store
    rating = CompanyRating(
        source='ambitionbox',
        company='Synthetic',
        url='https://www.ambitionbox.com/reviews/synthetic-reviews',
        company_id='1',
        overall_rating=4,
    )
    rating.identity_verified = True

    from applicant.infra.session_access import active_access

    def attempt():
        if public and active_access.get() is not None:
            raise SessionExpired('synthetic sign-out')
        return rating

    def fetch(*args, **kwargs):
        value, client.access = run_with_session(store, attempt, public=True)
        if public:
            ready.set()
            assert changed.wait(20)
        return value

    def emit(event):
        from applicant.services.events import RatingFetched

        if isinstance(event, RatingFetched) and not public:
            ready.set()
            assert changed.wait(20)

    monkeypatch.setattr(client, 'fetch', fetch)
    child.start()
    try:
        fetch_reviews('synthetic', {'ambitionbox': client}, str(output), emit=emit)
        child.join(20)
        assert child.exitcode == 0
    finally:
        client.close()
        if child.is_alive():
            child.terminate()
            child.join()
        child.close()
    cache = CompanyCache(output)
    current = store.snapshot()
    for record in (original, current):
        key = f'authenticated:default:{record["profile_id"]}:{record["generation"]}'
        assert cache.entry('ambitionbox', 'synthetic', key) is None
    assert (cache.entry('ambitionbox', 'synthetic', 'public:default') is not None) == public
