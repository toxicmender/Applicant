"""Run the CLI with intercepted provider responses in a separate process."""

import sys
from unittest.mock import patch

from applicant.cli import main
from applicant.errors import Blocked
from applicant.infra.browser import BrowserSession
from applicant.services.provider_setup import _verify_detailed


def fixture_cli(scenario, args):
    args = ['--no-log-file', *args]
    if scenario == 'acquisition':
        with patch('applicant.services.reviews.fetch_reviews', side_effect=Blocked('fixture')):
            return main([*args, 'reviews', 'synthetic'])

    markup = {
        'challenge': '<title>Just a moment</title>',
        'signout': '<a data-gnav-element-name="SignIn">Sign in</a>',
        'unknown': '<main>Unrecognized layout</main>',
    }[scenario]

    class FixtureSession(BrowserSession):
        def start(self):
            page = super().start()
            page.route('**/*', lambda route: route.fulfill(content_type='text/html', body=markup))
            return page

    def check(name, state, **options):
        return _verify_detailed(name, state, factory=FixtureSession, **options)

    from tests.fakes import RealBrowserTest

    harness = RealBrowserTest()
    harness.setUp()
    try:
        with patch('applicant.services.provider_setup._check_browser', side_effect=check):
            return main([*args, 'setup', 'status', '--provider', 'indeed', '--check'])
    finally:
        harness.doCleanups()


if __name__ == '__main__':
    raise SystemExit(fixture_cli(sys.argv[1], sys.argv[2:]))
