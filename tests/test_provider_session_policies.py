import json
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from applicant.domain.canonical import content_hash
from applicant.domain.providers import provider
from applicant.errors import AuthFailed, ConfigError, ProfileBusy
from applicant.infra.owned_worker import CleanupUnconfirmed, WorkerFailure
from applicant.services.provider_setup import (
    _check_browser,
    _verification_worker,
    _verify_detailed,
    import_session,
    provider_session,
    reconcile_session,
    session_status,
    setup_provider,
    validate_state,
)
from applicant.settings import Settings


@pytest.mark.parametrize(
    'name', ['indeed', 'naukri', 'googlejobs', 'glassdoor', 'ambitionbox', 'levels']
)
def test_provider_checks_expected_route_and_visible_markers(name):
    policy = provider(name).session
    assert policy is not None
    target = policy.targets[0]
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    page = session.start.return_value
    page.url = target.url
    page.title.return_value = 'Account'
    page.locator.return_value.count.return_value = 0
    page.evaluate.return_value = {}
    state = {'cookies': [], 'origins': []}
    session.context.storage_state.return_value = state
    assert _verify_detailed(name, state, factory=factory)['status'] == 'verified'
    for marker in target.markers:
        page.locator.assert_any_call(f':is({marker}):visible')


def test_glassdoor_cookies_keep_the_original_region():
    state = {
        'cookies': [
            {'name': 'session', 'value': 'synthetic', 'domain': '.glassdoor.co.in', 'path': '/'}
        ],
        'origins': [],
    }
    assert validate_state(state, 'glassdoor')[0]['cookies'][0]['domain'] == '.glassdoor.co.in'


def test_challenge_does_not_report_expiry():
    factory = MagicMock()
    page = factory.return_value.__enter__.return_value.start.return_value
    policy = provider('naukri').session
    assert policy is not None
    page.url = policy.targets[0].url
    page.title.return_value = 'Just a moment'
    assert _verify_detailed('naukri', {}, factory=factory) == {
        'status': 'inconclusive',
        'reason': 'challenge',
    }


@pytest.mark.parametrize(
    'status,code',
    [
        ({'status': 'needs_setup'}, 3),
        ({'status': 'inconclusive', 'reason': 'challenge'}, 11),
        ({'status': 'inconclusive', 'reason': 'marker_missing'}, 5),
        ({'status': 'inconclusive', 'reason': 'browser_unavailable'}, 7),
        (
            {
                'status': 'needs_setup',
                'health_recovery': {'status': 'database_busy', 'retryable': True},
            },
            8,
        ),
    ],
)
def test_session_cli_distinguishes_verification_outcomes(status, code):
    from applicant.cli import exit_code_for
    from applicant.cli.setup import output_result
    from applicant.errors import Blocked

    assert output_result({'provider': 'naukri', **status}) == code
    assert exit_code_for(Blocked('challenge')) == 3


@pytest.mark.parametrize(
    'name', ['indeed', 'naukri', 'googlejobs', 'glassdoor', 'ambitionbox', 'levels']
)
def test_visible_signed_out_controls_establish_expiry(name):
    policy = provider(name).session
    assert policy is not None
    target = policy.targets[0]
    assert target.signed_out
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    page = session.start.return_value
    page.url = target.url
    page.title.return_value = 'Account'

    def locator(selector):
        control = MagicMock()
        control.count.return_value = 0
        control.first.is_visible.return_value = selector in {
            f':is({s}):visible' for s in target.signed_out
        }
        return control

    page.locator.side_effect = locator
    assert _verify_detailed(name, None, factory=factory) == {
        'status': 'needs_setup',
        'reason': 'signed_out',
    }


def test_glassdoor_legacy_region_probe_tries_other_supported_origin():
    policy = provider('glassdoor').session
    assert policy is not None
    india, us = policy.targets
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    page = session.start.return_value
    page.url = 'about:blank'
    page.title.return_value = 'Account'
    page.locator.return_value.count.return_value = 0
    page.evaluate.return_value = {}
    visits = []

    def goto(url, **_):
        visits.append(url)
        page.url = 'https://www.glassdoor.com/login' if url == us.url else india.url

    page.goto.side_effect = goto
    state = {'cookies': [], 'origins': []}
    session.context.storage_state.return_value = state
    assert _verify_detailed('glassdoor', state, factory=factory)['region'] == 'co.in'
    assert visits == [us.url, india.url]


def test_headed_retry_and_capture_restore_keep_saved_session(tmp_path):
    settings = Settings(data_dir=tmp_path, store='files', session_storage='plaintext')
    state = {
        'cookies': [
            {'name': 'account', 'value': 'synthetic', 'domain': '.glassdoor.com', 'path': '/'}
        ],
        'origins': [],
    }
    verified = {
        'status': 'verified',
        'state': state,
        'region': 'com',
        'capture': {'locale': 'en-US'},
    }
    challenge = {'status': 'inconclusive', 'reason': 'challenge'}
    with (
        patch('sys.stdin.isatty', return_value=True),
        patch(
            'applicant.services.provider_setup._check_browser',
            side_effect=[verified, verified, challenge, verified],
        ) as browser,
    ):
        assert setup_provider(settings, 'glassdoor', region='com')['status'] == 'verified'
        store = provider_session(settings, 'glassdoor')
        record = store.snapshot()
        result = session_status(settings, 'glassdoor', check=True)
        assert result['reason'] == 'challenge'
        assert result['last_verified_at'] == record['checked_at']
        assert store.snapshot() == record
        assert session_status(settings, 'glassdoor', check=True, show=True)['status'] == 'verified'
        assert browser.call_args_list[1].kwargs['show']
        assert browser.call_args.kwargs['show']
        assert browser.call_args.kwargs['capture'] == {'locale': 'en-US'}


@pytest.mark.parametrize(
    ('destination', 'reason'),
    [
        ('https://levels.fyi.example/community', 'unexpected_page'),
        ('https://www.levels.fyi/community', 'marker_missing'),
    ],
)
def test_unrecognized_pages_cannot_expire_a_credential(destination, reason):
    factory = MagicMock()
    page = factory.return_value.__enter__.return_value.start.return_value
    page.url = 'about:blank'
    page.title.return_value = 'Account'
    page.locator.return_value.count.return_value = 0
    page.locator.return_value.first.is_visible.return_value = False
    page.goto.side_effect = lambda *_args, **_opts: setattr(page, 'url', destination)
    with patch('applicant.services.provider_setup.monotonic', side_effect=[0, 2, 11]):
        result = _verify_detailed('levels', None, factory=factory)
        assert result['status'] == 'inconclusive'
        assert result['reason'] == reason
    page.wait_for_timeout.assert_called_once_with(100)


@pytest.mark.parametrize('redirect_succeeds', [False, True])
def test_tracxn_finishes_the_login_redirect_without_a_second_navigation(redirect_succeeds):
    factory = MagicMock()
    session = factory.return_value.__enter__.return_value
    page = session.start.return_value
    page.url = 'https://tracxn.com/login'
    page.title.return_value = 'Dashboard'
    page.locator.return_value.count.return_value = 0
    page.evaluate.return_value = {}
    session.context.storage_state.return_value = {'cookies': [], 'origins': []}

    def redirect(*_args, **_opts):
        if not redirect_succeeds:
            raise PlaywrightTimeout('redirect still pending')
        page.url = 'https://platform.tracxn.com/a/dashboard'

    page.wait_for_url.side_effect = redirect
    result = _verify_detailed(
        'tracxn', None, interactive=True, factory=factory, pause=lambda _: None
    )
    assert result['status'] == ('verified' if redirect_succeeds else 'inconclusive')
    page.goto.assert_called_once_with('https://tracxn.com/login', wait_until='domcontentloaded')


def test_worker_unavailability_preserves_inconclusive_outcome():
    with patch('applicant.infra.owned_worker.run_worker', side_effect=WorkerFailure('offline')):
        assert _check_browser('naukri', None)['reason'] == 'browser_unavailable'
    with (
        patch('applicant.infra.owned_worker.run_worker', side_effect=CleanupUnconfirmed('worker')),
        pytest.raises(CleanupUnconfirmed),
    ):
        _check_browser('naukri', None)


def test_legacy_glassdoor_verification_records_region_after_migration(tmp_path):
    settings = Settings(data_dir=tmp_path, store='files', session_storage='plaintext')
    store = provider_session(settings, 'glassdoor')
    store.path.parent.mkdir()
    state = {
        'cookies': [
            {'name': 'account', 'value': 'synthetic', 'domain': '.glassdoor.co.in', 'path': '/'}
        ],
        'origins': [],
    }
    store.path.write_text(json.dumps(state), encoding='utf-8')
    store.path.with_suffix('.status.json').write_text(
        json.dumps({'status': 'verified', 'state_hash': content_hash(state)}),
        encoding='utf-8',
    )
    assert session_status(settings, 'glassdoor')['status'] == 'verified'
    with pytest.raises(ConfigError, match='managed session file'):
        import_session(settings, 'glassdoor', store.path)
    with patch(
        'applicant.services.provider_setup._check_browser',
        return_value={
            'status': 'verified',
            'state': state,
            'region': 'co.in',
        },
    ):
        assert session_status(settings, 'glassdoor', check=True)['status'] == 'verified'
    assert store.snapshot()['region'] == 'co.in'


def test_public_expiry_reconciliation_keeps_acquisition_available(tmp_path):
    settings = Settings(data_dir=tmp_path, session_storage='plaintext')
    store = provider_session(settings, 'levels.fyi')
    assert store.path == provider_session(settings, 'levels').path
    store = provider_session(settings, 'indeed')
    state = {
        'cookies': [
            {'name': 'account', 'value': 'synthetic', 'domain': '.indeed.com', 'path': '/'}
        ],
        'origins': [],
    }
    store.install(store.snapshot(), state, 'needs_setup', pending='expire')
    expired = {'provider': 'indeed', 'status': 'needs_setup'}
    assert session_status(settings, 'indeed', check=True)['health_recovery'] == {
        'status': 'credential_needs_setup'
    }
    store.clear()
    assert reconcile_session(settings, store, None, expired)['status'] == 'needs_setup'


def test_unsupported_provider_and_region_are_configuration_errors(tmp_path):
    settings = Settings(data_dir=tmp_path, store='files')
    with pytest.raises(ConfigError, match='unknown provider'):
        provider_session(settings, 'unknown')
    with pytest.raises(ConfigError, match='unsupported verification region'):
        setup_provider(settings, 'glassdoor', region='invalid')


@pytest.mark.parametrize('closed', [True, False])
def test_worker_checks_the_browser_while_waiting_for_user_login(closed):
    connection = MagicMock()
    connection.poll.side_effect = [False, True]
    connection.recv.return_value = ''
    page = MagicMock()
    page.is_closed.return_value = closed

    def detailed(_name, _state, *, factory, pause, interactive, **_):
        assert interactive
        with factory(headless=False) as browser:
            assert browser.start() is page
            pause('Finish login')
        return {'status': 'verified'}

    def run():
        return _verification_worker(connection, ('indeed', None, True, {'show': True}))

    with (
        patch('applicant.services.provider_setup.BrowserSession.start', return_value=page),
        patch('applicant.services.provider_setup._verify_detailed', side_effect=detailed),
    ):
        if closed:
            with pytest.raises(AuthFailed, match='browser closed'):
                run()
        else:
            result = run()
            assert result is not None
            assert result['status'] == 'verified'
            page.wait_for_timeout.assert_called_once_with(50)
    connection.send.assert_any_call(('closing', None))


def test_setup_refuses_late_verified_state_after_clear(tmp_path):
    settings = Settings(data_dir=tmp_path, store='files', session_storage='plaintext')
    store = provider_session(settings, 'indeed')
    state = {
        'cookies': [
            {'name': 'account', 'value': 'synthetic', 'domain': '.indeed.com', 'path': '/'}
        ],
        'origins': [],
    }

    def verify(*_args, **_opts):
        store.clear()
        return state

    with (
        patch('sys.stdin.isatty', return_value=True),
        pytest.raises(ProfileBusy, match='changed during verification'),
    ):
        setup_provider(settings, 'indeed', verifier=verify)
    assert store.snapshot()['status'] == 'cleared'
