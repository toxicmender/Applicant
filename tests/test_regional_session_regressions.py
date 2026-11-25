import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from applicant.boards.indeed import Indeed
from applicant.errors import Blocked
from applicant.infra.owned_worker import run_worker
from applicant.infra.session_access import active_access, run_with_session
from applicant.infra.session_store import SessionStore
from applicant.services.provider_setup import _verify_detailed, validate_state

UK = 'https://uk.indeed.com'
LOOKALIKE = UK + '.example.test'


def cookie(name='account', domain='uk.indeed.com', path='/'):
    return {'name': name, 'value': 'synthetic', 'domain': domain, 'path': path}


def state(*cookies):
    return {'cookies': list(cookies), 'origins': []}


def verification_site(captured, outcome, requested):
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    page = session.start.return_value
    page.url = 'about:blank'
    session.context.storage_state.return_value = captured
    page.evaluate.return_value = {}
    page.locator.return_value.count.return_value = 0

    def visit(url, **_):
        requested.append(url)
        page.url = url if outcome != 'lookalike' else LOOKALIKE + '/login'
        signed_in = url == UK + '/' and outcome == 'verified'
        page.title.return_value = 'Just a moment' if outcome == 'challenge' else 'Indeed'
        page.locator.return_value.first.is_visible.return_value = signed_in
        if url != UK + '/' or outcome == 'signed_out':
            page.url = url + 'login'

    page.goto.side_effect = visit
    return factory


@pytest.mark.parametrize(
    'domain, origin', [('uk.indeed.com', UK), ('uk.indeed.com', None), ('.indeed.com', UK)]
)
def test_indeed_verifies_regional_cookie_in_fresh_context(domain, origin):
    captured = state(cookie(domain=domain))
    requested = []
    options = {'origin': origin} if origin else {}
    first = _verify_detailed(
        'indeed', captured, factory=verification_site(captured, 'verified', requested), **options
    )
    assert requested == [UK + '/']
    assert first['status'] == 'verified'
    assert first['origin'] == UK


def test_indeed_rejects_lookalike_verification_origin():
    captured = state(cookie())
    requested = []
    with patch('applicant.services.provider_setup.monotonic', side_effect=[0, 20]):
        result = _verify_detailed(
            'indeed',
            captured,
            origin=LOOKALIKE,
            factory=verification_site(captured, 'lookalike', requested),
        )
    assert requested == [UK + '/']
    assert result == {'status': 'inconclusive', 'reason': 'unexpected_page'}


def test_indeed_parent_domain_import_needs_regional_signout_evidence():
    captured = state(cookie(domain='.indeed.com'))
    requested = []
    result = _verify_detailed(
        'indeed', captured, factory=verification_site(captured, 'signed_out', requested)
    )
    assert requested == ['https://in.indeed.com/']
    assert result == {'status': 'inconclusive', 'reason': 'unexpected_page'}


@pytest.mark.parametrize('outcome', ['verified', 'challenge', 'signed_out'])
def test_indeed_403_verifies_regional_session_before_public_retry(tmp_path, monkeypatch, outcome):
    store = SessionStore(tmp_path / 'storage_state.json', 'indeed', mode='plaintext')
    captured, _ = validate_state(state(cookie()), 'indeed')
    installed = store.install(
        {}, captured, 'verified', metadata={'origin': UK, 'checked_at': 'saved'}
    )
    requested = []
    attempts = []
    verification = verification_site(captured, outcome, requested)
    jobs = {'metaData': {'mosaicProviderJobCardsModel': {'results': []}}}
    mosaic = 'window.mosaic.providerData["mosaic-provider-jobcards"] = ' + json.dumps(jobs) + ';'

    def transport(request):
        signed_in = request.headers.get('cookie') == 'account=synthetic'
        attempts.append((signed_in, request.url.host, request.url.params.get('start')))
        return httpx.Response(403) if signed_in else httpx.Response(200, text=mosaic)

    @contextmanager
    def browser(**_):
        assert active_access.get() is not None
        page = MagicMock()
        page.title.return_value = 'Indeed'
        page.locator.return_value.count.return_value = 0
        page.content.return_value = mosaic
        yield page
        page.goto.assert_called_once()

    def attempt():
        with httpx.Client(transport=httpx.MockTransport(transport)) as http:
            client = Indeed(client=http, delay=0)
            try:
                return client.search('engineer', 'UK', 2)
            finally:
                client.close()

    monkeypatch.setattr('applicant.boards.indeed.browser', browser)
    monkeypatch.setattr(
        'applicant.services.provider_setup._verify_detailed',
        lambda *args, **kwargs: _verify_detailed(*args, **kwargs, factory=verification),
    )
    if outcome == 'challenge':
        with pytest.raises(Blocked):
            run_with_session(store, attempt, public=True)
    else:
        _, access = run_with_session(store, attempt, public=True)
        assert access['access_mode'] == ('public' if outcome == 'signed_out' else 'authenticated')
    assert requested == [UK + '/']
    assert attempts == (
        [(True, 'uk.indeed.com', None), (False, 'uk.indeed.com', None)]
        if outcome == 'signed_out'
        else [(True, 'uk.indeed.com', None)]
    )
    if outcome == 'signed_out':
        assert store.snapshot()['status'] == 'needs_setup'
    else:
        assert store.snapshot() == installed


def cookie_exchange_worker(_connection, arguments):
    path, delete, target = arguments
    store = SessionStore(path, 'indeed', mode='plaintext')
    seen = []

    def handle(request):
        seen.append(request.headers.get('cookie'))
        headers = {'Set-Cookie': 'account=; Path=/; Max-Age=0; Secure; HttpOnly'} if delete else {}
        return httpx.Response(200, headers=headers)

    def request():
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            access = active_access.get()
            if access is None:
                http.get(target)
            else:
                access.http(http, lambda: http.get(target), accepted=lambda reply: reply.is_success)

    run_with_session(store, request, public=True)
    return seen


@pytest.mark.parametrize('keep_other', [False, True])
def test_deleted_final_cookie_is_absent_in_a_fresh_process(tmp_path, keep_other):
    store = SessionStore(tmp_path / 'storage_state.json', 'indeed', mode='plaintext')
    cookies = [cookie()]
    if keep_other:
        cookies.append(cookie('other', path='/account'))
    assert store.install({}, state(*cookies), 'verified') is not None
    assert run_worker(cookie_exchange_worker, (store.path, True, UK + '/jobs')) == [
        'account=synthetic'
    ]
    assert run_worker(cookie_exchange_worker, (store.path, False, UK + '/account')) == [
        'other=synthetic' if keep_other else None
    ]
