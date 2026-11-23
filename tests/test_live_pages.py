"""The browser-driven sources in a real Chromium, against saved pages.

tests/test_board_flows.py and friends run the same flows on a scripted fake;
here the selectors, the page's own API calls heard through
`page.on('response')`, lazy loading on scroll and `inner_text`'s line layout
are the real thing. Every request is answered from tests/fixtures/pages, so
nothing reaches the network.

The pages are written to the markup the code expects - the live sites cannot
be reached from CI - so they check the code against its own assumptions, not
against today's live markup. `tools/record_page.py` saves a live page to
replace one.

Skipped without a browser (see RealBrowserTest); CI's browser job installs one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

import httpx

from applicant.boards.googlejobs import GoogleJobs
from applicant.boards.indeed import Indeed
from applicant.boards.linkedin_apply import LinkedIn
from applicant.boards.naukri import Naukri
from applicant.domain.providers import provider
from applicant.errors import Blocked, NotFound
from applicant.financials import CrunchbaseClient, TracxnClient
from applicant.infra.browser import BrowserSession
from applicant.reviews import GlassdoorClient
from applicant.services.provider_setup import _verify_detailed
from tests.fakes import RealBrowserTest

PAGE = 200


class NaukriLiveTest(RealBrowserTest):
    routes = (
        (r'/jobapi/v3/search', (PAGE, 'naukri/search-1.json', {})),
        (r'naukri\.com/python-jobs$', (PAGE, 'naukri/search-1.html', {})),
        (r'naukri\.com/python-jobs-2$', (PAGE, 'naukri/search-2.html', {})),
        (r'naukri\.com/python-jobs-3$', (PAGE, 'naukri/search-3.html', {})),
    )

    def test_its_api_call_then_the_rendered_tuples_then_the_end(self):
        jobs = Naukri(delay=0).search('python', limit=10)
        self.assertEqual([job.id for job in jobs], ['101', '102', '103'])
        first, third = jobs[0], jobs[2]
        self.assertEqual(
            (first.location, first.salary, first.experience_text),
            ('Pune', '10-15 Lacs PA', '2-5 Yrs'),
        )
        self.assertEqual(first.url, 'https://www.naukri.com/job-listings-python-developer-acme-101')
        self.assertEqual(
            (third.company, third.location, third.salary), ('Gamma Analytics', 'Hyderabad', None)
        )
        self.assertEqual(third.url, 'https://www.naukri.com/job-listings-data-engineer-gamma-103')


class GoogleJobsLiveTest(RealBrowserTest):
    routes = ((r'google\.com/search', (PAGE, 'googlejobs/results.html', {})),)

    def test_scrolls_for_more_cards_and_reads_each_one_s_lines(self):
        jobs = GoogleJobs().search('python', 'Pune', limit=20)
        self.assertEqual(len(jobs), 20)
        job = jobs[0]
        self.assertEqual(
            (job.title, job.company, job.via), ('Python Developer 0', 'Acme', 'LinkedIn')
        )
        self.assertEqual(
            (job.location, job.employment_type, job.posted_text),
            ('Pune, Maharashtra', 'Full-time', '1 days ago'),
        )


class IndeedLiveTest(RealBrowserTest):
    routes = ((r'indeed\.com/jobs', (PAGE, 'indeed/results.html', {})),)

    def test_a_refused_request_is_read_in_the_browser(self):
        refused = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
        client = Indeed(domain='https://www.indeed.com', delay=0, client=refused)
        with self.assertLogs('applicant.boards.indeed', 'INFO'):
            jobs = client.search('python', 'Pune', limit=5)
        self.assertEqual([job.id for job in jobs], ['k1', 'k2'])


class CrunchbaseLiveTest(RealBrowserTest):
    routes = (
        (
            r'organization/zomato/company_financials$',
            (PAGE, 'crunchbase/company_financials.html', {}),
        ),
        (r'organization/zomato$', (PAGE, 'crunchbase/organization.html', {})),
    )

    def client(self) -> CrunchbaseClient:
        return CrunchbaseClient(api_key='', profile_dir=str(self.folder / 'cb'), timeout=10)

    def test_the_page_state_and_its_text(self):
        result = self.client().fetch('zomato')
        assert result.total_funding is not None
        self.assertEqual(result.total_funding.amount_usd, 2.1e9)
        self.assertEqual(result.funding_rounds_count, 20)
        self.assertEqual(
            (result.last_funding_type, result.revenue_range), ('Post-IPO Equity', '$1B to $10B')
        )
        self.assertEqual(result.employees, '5001-10000')
        self.assertEqual([item.round for item in result.rounds], ['Series J', 'Series E'])

    def test_an_organization_that_is_not_there(self):
        with self.assertRaises(NotFound):
            self.client().fetch('no-such-company')


class TracxnLiveTest(RealBrowserTest):
    profile = 'https://tracxn.com/d/companies/zomato/__DVekVOT4DO1dVZeaG1vMa1V9'
    routes = (
        (r'tracxn\.com/api/companies/funding-rounds', (PAGE, 'tracxn/rounds.json', {})),
        (r'__\w+/funding-and-investors$', (404, None, {})),
        (r'__\w+$', (PAGE, 'tracxn/profile.html', {})),
    )

    def test_the_profile_its_api_call_and_its_text(self):
        client = TracxnClient(api_key='', profile_dir=str(self.folder / 'tx'), timeout=10)
        result = client.fetch(self.profile)
        self.assertEqual(result.company, 'Zomato')
        self.assertEqual([r.round for r in result.rounds], ['Series J', 'Series I'])
        assert result.valuation is not None
        self.assertEqual(result.valuation.amount, 8e9)
        self.assertEqual(result.employees, '5000')


class GlassdoorLiveTest(RealBrowserTest):
    routes = (
        (r'glassdoor\.com/graph', (PAGE, 'glassdoor/graph.json', {})),
        (r'-Reviews-E9079_P2\.htm', (PAGE, 'glassdoor/reviews-2.html', {})),
        (r'-Reviews-E9079\.htm', (PAGE, 'glassdoor/reviews-1.html', {})),
    )

    def test_its_graph_call_then_the_embedded_state_then_a_missing_page(self):
        client = GlassdoorClient(profile_dir=str(self.folder / 'gd'), timeout=10_000)
        with self.assertLogs('applicant', 'INFO'):
            rating = client.fetch('Google-E9079', max_reviews=10)
        self.assertEqual((rating.overall_rating, rating.review_count), (4.4, 12345))
        # from both the graph call and the page's own state, each once
        self.assertCountEqual(
            [review.pros for review in rating.reviews], ['Smart colleagues', 'Pay', 'Food']
        )


class LinkedInLiveTest(RealBrowserTest):
    routes = (
        (r'linkedin\.com/feed/', (PAGE, 'linkedin/redirect-login.html', {})),
        (r'linkedin\.com/jobs/view/', (PAGE, 'linkedin/redirect-checkpoint.html', {})),
    )

    def setUp(self):
        super().setUp()
        self.client = LinkedIn(delay=0)
        self.addCleanup(self.client.close)

    def test_an_expired_session_is_redirected_to_the_login(self):
        state = self.folder / 'cookies.json'
        state.write_text(json.dumps({'cookies': [], 'origins': []}), encoding='utf-8')
        with self.assertLogs('applicant', 'WARNING') as logged:
            self.assertFalse(self.client.restore_session(state))
        self.assertIn('has expired', '\n'.join(logged.output))

    def test_a_checkpoint_on_a_job_page_stops_applying(self):
        page = self.client._start()
        posting = {'url': 'https://www.linkedin.com/jobs/view/42', 'title': 'ML'}
        with self.assertRaisesRegex(Blocked, 'checkpoint'):
            self.client._apply_one(page, posting)
        # the job page and nothing after it: no form opened, nothing submitted
        pages = [url for url in self.requested if not url.endswith('/favicon.ico')]
        self.assertEqual(pages, [posting['url']])

    def test_guard_accepts_a_shadow_button_that_submits_via_javascript(self):
        from applicant.boards.linkedin_apply import _prepare_submit

        page = self.client._start()
        page.set_content("""
            <div role="dialog" aria-modal="true"><div id="host"></div></div>
            <script>
                window.submissions = 0;
                const root = document.getElementById('host').attachShadow({mode: 'open'});
                root.innerHTML = '<button type="button"><span>Submit application</span></button>';
                root.querySelector('button').onclick = () => window.submissions++;
            </script>
        """)
        dispatch = _prepare_submit(page)
        self.assertIsNotNone(dispatch)
        assert dispatch is not None
        self.assertTrue(dispatch())
        self.assertEqual(page.evaluate('window.submissions'), 1)

    def test_replaced_submit_control_does_not_receive_input(self):
        from applicant.boards.linkedin_apply import _prepare_submit

        page = self.client._start()
        page.set_content("""
            <div role="dialog" aria-modal="true">
                <button type="button">Submit application</button>
            </div>
        """)
        dispatch = _prepare_submit(page)
        self.assertIsNotNone(dispatch)
        assert dispatch is not None
        page.locator('button').evaluate('button => button.replaceWith(button.cloneNode(true))')
        self.assertFalse(dispatch())


if __name__ == '__main__':
    unittest.main()


AUTH_FIXTURES = Path(__file__).parent / 'fixtures' / 'provider_auth'


class ProviderVerificationBrowserTest(RealBrowserTest):
    def test_signed_in_fixtures_and_hidden_guest_controls(self):
        def verify(name, fixture):
            markup = (AUTH_FIXTURES / f'{fixture}.html').read_text(encoding='utf-8')

            class FixtureSession(BrowserSession):
                def start(self):
                    page = super().start()
                    page.route(
                        '**/*', lambda route: route.fulfill(content_type='text/html', body=markup)
                    )
                    return page

            return _verify_detailed(name, None, factory=FixtureSession)['status']

        for name in ('indeed', 'naukri', 'googlejobs', 'glassdoor', 'ambitionbox', 'levels'):
            with self.subTest(provider=name):
                policy = provider(name).session
                assert policy is not None
                assert verify(name, name) == 'verified'
        assert verify('levels', 'levels-signed-out') == 'needs_setup'

    def test_indeed_restores_a_captured_uk_cookie_in_a_fresh_browser(self):
        origin = 'https://uk.indeed.com'
        markup = (AUTH_FIXTURES / 'indeed.html').read_text(encoding='utf-8')
        requests = []
        page = None
        capturing = True

        def respond(route):
            if not route.request.is_navigation_request():
                route.abort()
                return
            url = route.request.url
            cookie = route.request.header_value('cookie')
            requests.append((url, cookie))
            if url == origin + '/' and (capturing or cookie == 'account=synthetic'):
                headers = (
                    {'Set-Cookie': 'account=synthetic; Path=/; Secure; HttpOnly'}
                    if capturing
                    else {}
                )
                route.fulfill(content_type='text/html', body=markup, headers=headers)
            else:
                route.fulfill(
                    content_type='text/html',
                    body='<a data-gnav-element-name="SignIn">Sign in</a>',
                )

        class FixtureSession(BrowserSession):
            def start(self):
                nonlocal page
                if self._page is None:
                    page = super().start()
                    page.route('**/*', respond)
                return super().start()

        def fixture_session(*, headless, storage_state, capture_settings):
            return FixtureSession(
                headless=True, storage_state=storage_state, capture_settings=capture_settings
            )

        def sign_in(_prompt):
            assert page is not None
            page.goto(origin + '/')

        captured = _verify_detailed(
            'indeed', None, interactive=True, factory=fixture_session, pause=sign_in
        )
        assert captured['status'] == 'verified'
        state = captured['state']
        assert isinstance(state, dict)
        assert state['cookies'][0]['domain'] == 'uk.indeed.com'
        capturing = False
        requests.clear()
        restored = _verify_detailed(
            'indeed', state, origin=captured['origin'], factory=fixture_session
        )
        assert restored['status'] == 'verified'
        assert requests == [(origin + '/', 'account=synthetic')]

    def test_cli_verification_outcomes_in_a_fresh_process(self):
        from applicant.services.provider_setup import provider_session
        from applicant.settings import Settings

        store = provider_session(
            Settings(data_dir=self.folder, session_storage='plaintext', store='files'), 'indeed'
        )
        store.install(
            {},
            {
                'cookies': [
                    {
                        'name': 'account',
                        'value': 'synthetic',
                        'domain': '.indeed.com',
                        'path': '/',
                        'expires': -1,
                        'secure': True,
                        'httpOnly': False,
                        'sameSite': 'Lax',
                    }
                ],
                'origins': [],
            },
            'verified',
            metadata={
                'checked_at': '2026-11-01T00:00:00+00:00',
                'origin': 'https://in.indeed.com',
            },
        )
        config = self.folder / 'config.toml'
        config.write_text('session_storage = "plaintext"\nstore = "files"\n', encoding='utf-8')
        original = store.path.read_bytes()
        for page, code, status, reason in (
            ('challenge', 11, 'inconclusive', 'challenge'),
            ('unknown', 5, 'inconclusive', 'marker_missing'),
            ('signout', 3, 'needs_setup', None),
            ('acquisition', 3, None, None),
        ):
            with self.subTest(page=page):
                command = subprocess.run(
                    [
                        sys.executable,
                        '-m',
                        'tests.provider_browser_cli',
                        page,
                        '--config',
                        str(config),
                        '--data-dir',
                        str(self.folder),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                assert command.returncode == code, (command.stdout, command.stderr)
                if status:
                    result = json.loads(command.stdout)
                    assert result['status'] == status
                    assert result.get('reason') == reason
                if page in {'challenge', 'unknown'}:
                    assert store.path.read_bytes() == original
