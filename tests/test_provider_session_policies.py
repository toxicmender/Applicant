from unittest.mock import MagicMock

import pytest

from applicant.domain.providers import provider
from applicant.services.provider_setup import _verify_detailed, validate_state


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
