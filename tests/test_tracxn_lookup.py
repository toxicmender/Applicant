import io
import json
import os
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from playwright._impl._errors import TargetClosedError

from applicant.cli import main
from applicant.cli.tracxn_lookup import InteractiveTracxn, LookupStopped, SessionEnded, company_name
from applicant.domain.job import Job
from applicant.errors import ProfileBusy, SourceError
from applicant.financials import TracxnClient
from applicant.financials.tracxn import TracxnLookupRequired, confirmed_profile_url
from applicant.interaction import Scripted, Terminal
from applicant.storage import save_jobs

PROFILE = 'https://platform.tracxn.com/d/companies/booz-allen/__profile-1'
SIGNED_IN_PROFILE = 'https://platform.tracxn.com/a/d/company/profile-1/booz%20allen'
DASHBOARD = 'https://platform.tracxn.com/a/dashboard'
COMPANY = 'Booz Allen Hamilton'


class Tab:
    def __init__(self):
        self.url = DASHBOARD
        self.closed = False
        self.main_frame = object()
        self.context = Mock(pages=[self])
        self.listeners = {}
        self.visited = []
        self.heading = 'Booz Allen Hamilton, Inc.'
        self.page_title = 'Booz Allen Hamilton - Company Profile'
        self.profile_card_links = []

    def on(self, event, callback):
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event, callback):
        self.listeners[event].remove(callback)

    def goto(self, url, *, wait_until='commit', status=200):
        self.url = url
        self.visited.append(url)
        response = Mock(url=url, status=status, frame=self.main_frame)
        response.request.is_navigation_request.return_value = True
        for callback in self.listeners.get('response', []):
            callback(response)
        for callback in self.listeners.get('framenavigated', []):
            callback(self.main_frame)
        return response

    def is_closed(self):
        return self.closed

    def title(self):
        return self.page_title

    def locator(self, selector):
        node = Mock()
        node.count.return_value = 0
        node.all_text_contents.return_value = [self.heading] if selector == 'h1:visible' else []
        node.evaluate_all.return_value = self.profile_card_links
        return node

    def wait_for_load_state(self, *args, **kwargs):
        pass

    def wait_for_timeout(self, timeout):
        pass

    def content(self):
        return f'<main><h1>{self.heading}</h1><div>Annual Revenue</div><div>$2M</div></main>'

    def inner_text(self, selector):
        return f'{self.heading}\nAnnual Revenue\n$2M'


class BrowserInteraction(Scripted):
    def __init__(self, *steps: str | Callable[[], str]):
        super().__init__()
        self.steps = iter(steps)

    def ask(self, question, *, secret=False):
        self.said.append(question)
        step = next(self.steps)
        return step() if callable(step) else step


class TracxnLookupTest(unittest.TestCase):
    def setUp(self):
        self.page = Tab()
        self.session = Mock()
        self.session.start.return_value = self.page
        self.browser = Mock(return_value=self.session)
        self.client = TracxnClient(api_key='', profile_dir='custom-profile')

    def resolver(self, *steps, eligible=lambda: True):
        interaction = BrowserInteraction(*steps)
        resolver = InteractiveTracxn(
            self.client, interaction, 'test-reauth', eligible=eligible, browser_factory=self.browser
        )
        self.addCleanup(resolver.close)
        return resolver, interaction

    def visit(self, url=PROFILE, *, status=200, reply=''):
        def select():
            self.page.goto(url, status=status)
            return reply

        return select

    def test_normalization_and_canonical_routes(self):
        self.assertEqual(company_name('  Ａ&B-Hamilton, Inc. '), company_name('a and b hamilton'))
        self.assertEqual(company_name('Alpha L.L.C.'), company_name('Alpha'))
        self.assertEqual(company_name('Booz Allen Hamilton (BAH:NYSE)'), company_name(COMPANY))
        self.assertNotEqual(
            company_name('Booz Allen Hamilton India (BAH:NYSE)'), company_name(COMPANY)
        )
        self.assertNotEqual(company_name('Alpha India Ltd.'), company_name('Alpha'))
        self.assertEqual(confirmed_profile_url(PROFILE + '/?token=secret#section'), PROFILE)
        self.assertEqual(
            confirmed_profile_url(SIGNED_IN_PROFILE + '/?token=secret#a:key-metrics'),
            SIGNED_IN_PROFILE,
        )
        self.assertEqual(
            confirmed_profile_url('https://platform.tracxn.com/a/d/company/profile-1'),
            'https://platform.tracxn.com/a/d/company/profile-1',
        )
        self.assertIsNotNone(confirmed_profile_url(PROFILE.replace('booz-allen', 'search')))
        for url in (
            PROFILE.replace('https://', 'http://'),
            PROFILE.replace('platform.tracxn.com', 'platform.tracxn.com.evil.example'),
            PROFILE.replace('https://', 'https://user@'),
            PROFILE + '/announcement',
            PROFILE.replace('booz-allen', '..'),
            PROFILE.replace('/d/companies/', '/d/search/'),
            'https://platform.tracxn.com:bad/d/companies/x/__abc',
            SIGNED_IN_PROFILE + '/claimprofile',
            SIGNED_IN_PROFILE.replace('booz%20allen', '%2e%2e'),
            SIGNED_IN_PROFILE.replace('booz%20allen', 'x%2fy'),
            SIGNED_IN_PROFILE.replace('platform.tracxn.com', 'tracxn.com'),
        ):
            with self.subTest(url=url):
                self.assertIsNone(confirmed_profile_url(url))

    def test_signed_in_profile_uses_entity_id_and_funding_section(self):
        self.page.page_title = 'Tracxn'
        resolver, interaction = self.resolver(
            'yes', self.visit(SIGNED_IN_PROFILE + '?private=ignored#a:key-metrics'), 'yes'
        )
        result = resolver.fetch(COMPANY)
        self.assertEqual(result.url, SIGNED_IN_PROFILE)
        self.assertEqual(result.company_id, 'profile-1')
        self.assertEqual(self.page.visited[-1], SIGNED_IN_PROFILE + '#a:funding-and-investors')
        self.assertNotIn('private=ignored', '\n'.join(interaction.said))

    def test_signed_in_profile_card_requires_a_link_to_the_open_company(self):
        self.page.page_title = 'Tracxn'
        self.page.heading = ''
        actual_profile = (
            'https://platform.tracxn.com/a/d/company/52d69058e4b07c90e4b084f3/boozallen.com'
        )
        wrong_profile = actual_profile.replace('52d69058e4b07c90e4b084f3', 'other-id')

        def select_link(url):
            self.page.profile_card_links = [[url, 'Booz Allen Hamilton (BAH:NYSE)']]
            return self.visit(actual_profile)()

        resolver, interaction = self.resolver(
            'yes', lambda: select_link(wrong_profile), lambda: select_link(actual_profile), 'yes'
        )
        result = resolver.fetch(COMPANY)
        self.assertEqual(result.url, actual_profile)
        self.assertIn('does not identify', '\n'.join(interaction.said))
        self.assertIn('Profile: Booz Allen Hamilton (BAH:NYSE)', '\n'.join(interaction.said))

    def test_noninteractive_and_declined_never_open_a_browser(self):
        for steps, eligible in (((), lambda: False), (('n',), lambda: True)):
            resolver, interaction = self.resolver(*steps, eligible=eligible)
            for _ in range(2):
                with self.assertRaises(TracxnLookupRequired):
                    resolver.fetch(COMPANY)
            self.assertEqual(len(interaction.said), len(steps))
        self.browser.assert_not_called()

    def test_rejects_wrong_pages_and_stale_confirmation_then_reads_profile(self):
        def mismatched():
            self.page.heading = 'Booz Allen Hamilton India'
            return ''

        def reselect():
            self.page.heading = 'Booz Allen Hamilton, Inc.'
            return self.visit()()

        def changed_title():
            self.page.page_title = 'Updated company profile'
            return 'yes'

        resolver, interaction = self.resolver(
            'yes',
            self.visit(DASHBOARD + '/search'),
            self.visit(status=404),
            self.visit(),
            'back',
            mismatched,
            reselect,
            changed_title,
            '',
            'yes',
            reselect,
            'yes',
        )
        result = resolver.fetch(COMPANY, max_rounds=1)
        self.assertEqual((result.url, result.company), (PROFILE, 'Booz Allen Hamilton, Inc.'))
        assert result.revenue is not None
        self.assertEqual(result.revenue.amount, 2_000_000)
        with self.assertRaisesRegex(SourceError, 'already recorded'):
            resolver.fetch(COMPANY)
        prompts = '\n'.join(interaction.said)
        self.assertIn('does not match', prompts)
        self.assertIn('Tab title:', prompts)
        self.assertIn('Selection was not confirmed', prompts)
        self.browser.assert_called_once()
        self.assertTrue(self.browser.call_args.kwargs['require_saved_state'])
        self.assertFalse(self.browser.call_args.kwargs['headless'])

    def test_session_expiry_and_browser_closure_stop_further_prompts(self):
        def closed():
            raise TargetClosedError()

        def crashed():
            for callback in self.page.listeners['crash']:
                callback(self.page)
            return ''

        for action, error, message in (
            (self.visit('https://tracxn.com/login'), SessionEnded, 'test-reauth'),
            (closed, LookupStopped, 'browser was closed'),
            (crashed, LookupStopped, 'tab crashed'),
            ('stop', LookupStopped, 'lookup stopped'),
        ):
            resolver, interaction = self.resolver('y', action)
            with self.assertRaisesRegex(error, message):
                resolver.fetch(COMPANY)
            with self.assertRaises(TracxnLookupRequired):
                resolver.fetch(COMPANY)
            self.assertEqual(len(interaction.said), 2)
        self.assertEqual(self.session.close.call_count, 4)

    def test_skip_and_wrong_page_title_leave_current_company_unresolved(self):
        def show_investor():
            self.page.page_title = 'Booz Allen Hamilton - Investor Profile'
            return self.visit()()

        def show_empty_search():
            self.page.page_title = 'Search results - Tracxn'
            self.page.heading = 'No Results Found'
            return ''

        resolver, interaction = self.resolver('yes', show_investor, show_empty_search, 'skip')
        with self.assertRaisesRegex(SourceError, 'Skipped'):
            resolver.fetch(COMPANY)
        self.assertEqual(sum('does not identify' in line for line in interaction.said), 2)
        self.session.close.assert_not_called()
        self.browser.assert_called_once()

    def test_wrong_tab_rejection_is_logged_without_url_query_or_title(self):
        self.page.context.pages.append(Mock())
        resolver, interaction = self.resolver(
            'yes', self.visit(DASHBOARD + '?token=private-value'), 'skip'
        )
        with (
            self.assertLogs('applicant.browser', 'DEBUG') as logs,
            self.assertRaisesRegex(SourceError, 'Skipped'),
        ):
            resolver.fetch(COMPANY)
        output = '\n'.join(logs.output)
        self.assertIn("'route': 'dashboard'", output)
        self.assertIn("'open_tabs': 2", output)
        self.assertIn("'reason': 'unsupported_profile_url'", output)
        self.assertNotIn('private-value', output)
        self.assertIn('Use that tab for the profile', '\n'.join(interaction.said))

    @unittest.skipUnless(os.name == 'nt', 'requires the Windows console module')
    def test_terminal_poll_releases_browser_when_closed_while_waiting(self):
        def close_tab(timeout):
            self.page.closed = True

        self.page.wait_for_timeout = close_tab
        resolver = InteractiveTracxn(
            self.client,
            Terminal(),
            'test-reauth',
            eligible=lambda: True,
            browser_factory=self.browser,
        )
        with (
            patch.object(Terminal, 'ask', return_value='yes'),
            patch('msvcrt.kbhit', return_value=False),
            redirect_stderr(io.StringIO()),
            self.assertRaisesRegex(LookupStopped, 'browser was closed'),
        ):
            resolver.fetch(COMPANY)
        self.session.close.assert_called_once()

    @unittest.skipUnless(os.name == 'nt', 'requires the Windows console module')
    def test_windows_command_input_can_edit_and_interrupt(self):
        with (
            patch('msvcrt.kbhit', return_value=True),
            patch('msvcrt.getwch', side_effect=['s', 'k', 'x', '\b', 'i', 'p', '\r', '\x03']),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(Terminal().ask_interruptibly('Command?', lambda: None), 'skip')
            with self.assertRaises(KeyboardInterrupt):
                Terminal().ask_interruptibly('Command?', lambda: None)

    def test_missing_and_locked_profile_have_actionable_errors(self):
        from applicant.errors import AuthFailed

        for error, expected, message in (
            (AuthFailed(), SessionEnded, 'test-reauth'),
            (ProfileBusy(), ProfileBusy, 'profile_busy: custom-profile'),
        ):
            self.session.start.side_effect = error
            resolver, _ = self.resolver('yes')
            with self.assertRaisesRegex(expected, message):
                resolver.fetch(COMPANY)

    def test_from_jobs_retains_completed_write_when_next_lookup_is_interrupted(self):
        def interrupt():
            raise KeyboardInterrupt

        interaction = BrowserInteraction('yes', self.visit(), 'yes', interrupt)
        with tempfile.TemporaryDirectory() as folder:
            listing = Path(folder) / 'jobs.json'
            history = Path(folder) / 'financials.json'
            save_jobs(
                [
                    Job(source='indeed', title='Engineer', company=COMPANY, id='first'),
                    Job(source='indeed', title='Engineer', company='Other Company', id='second'),
                ],
                str(listing),
            )
            with (
                patch('applicant.cli.financials.terminal', return_value=interaction),
                patch('applicant.cli.tracxn_lookup.terminal_available', return_value=True),
                patch('applicant.cli.tracxn_lookup.BrowserSession', self.browser),
                patch('applicant.settings.Settings.secret', return_value=''),
                patch.object(TracxnClient, '_fetch_api') as api,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                code = main(
                    [
                        '--no-log-file',
                        '--store',
                        'files',
                        'financials',
                        '--from-jobs',
                        str(listing),
                        '-s',
                        'tracxn',
                        '--output',
                        str(history),
                        '--tx-profile',
                        str(Path(folder) / 'custom'),
                    ]
                )
            self.assertEqual(code, 130)
            latest = next(
                iter(json.loads(history.read_text(encoding='utf-8'))['companies'].values())
            )['latest']
            self.assertEqual(latest['url'], PROFILE)
            self.assertEqual(latest['revenue']['amount'], 2_000_000)
            self.assertEqual(
                self.browser.call_args.kwargs['profile_dir'], str(Path(folder) / 'custom')
            )
            api.assert_not_called()
            self.session.close.assert_called_once()
