"""Execute normal setup CLI dispatch with Chromium requests answered entirely offline."""

import sys
from pathlib import Path
from unittest.mock import patch

from applicant.cli import main
from applicant.domain.providers import provider
from applicant.infra.browser import BrowserSession
from applicant.services.provider_setup import _verify_detailed
from tests.fakes import RealBrowserTest

FIXTURES = Path(__file__).parent / 'fixtures' / 'provider_auth'
PAGE_TEXT = 'synthetic-page-canary'
QUERY_VALUE = 'synthetic-query-canary'
COOKIE_VALUE = 'synthetic-cookie-canary'


def fixture_cli(name, scenario, args):
    policy = provider(name).session
    assert policy is not None
    if scenario == 'no-worker':
        with patch(
            'applicant.services.provider_setup._check_browser',
            side_effect=AssertionError('missing credential unexpectedly launched a browser'),
        ):
            return main(args)

    harness = RealBrowserTest()
    harness.setUp()
    phases = []
    try:

        def check(name, state, *, interactive=False, **options):
            phases.append('capture' if interactive else 'restore')
            markup = (FIXTURES / f'{name}.html').read_text(encoding='utf-8')
            if scenario == 'missing':
                markup = '<button data-testid="userProfile">Profile</button>'
            markup += '<main>' + PAGE_TEXT + '</main>'
            markup += f"<script>history.replaceState(null, '', '?fixture={QUERY_VALUE}')</script>"

            class Session(BrowserSession):
                def start(self):
                    if self._page is not None:
                        return super().start()
                    page = super().start()

                    def serve(route):
                        if (
                            interactive
                            or route.request.header_value('cookie') == 'account=' + COOKIE_VALUE
                        ):
                            route.fulfill(
                                content_type='text/html',
                                body=markup,
                                headers={
                                    'Set-Cookie': 'account=' + COOKIE_VALUE + '; Secure; Path=/'
                                },
                            )
                        else:
                            route.fulfill(content_type='text/html', body='<main>Guest</main>')

                    page.route('**/*', serve)
                    return page

            def factory(**options):
                options['headless'] = True
                return Session(**options)

            return _verify_detailed(
                name,
                state,
                interactive=interactive,
                factory=factory,
                pause=lambda _prompt: None,
                **options,
            )

        with (
            patch('sys.stdin.isatty', return_value=True),
            patch('applicant.services.provider_setup._check_browser', side_effect=check),
        ):
            code = main(args)
            if scenario == 'signed-in':
                assert phases == ['capture', 'restore'], (
                    f'{name} setup: expected separate capture and fresh-context verification'
                )
            return code
    finally:
        harness.doCleanups()


if __name__ == '__main__':
    raise SystemExit(fixture_cli(sys.argv[1], sys.argv[2], sys.argv[3:]))
