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
import unittest

import httpx

from applicant.boards.googlejobs import GoogleJobs
from applicant.boards.indeed import Indeed
from applicant.boards.linkedin_apply import LinkedIn
from applicant.boards.naukri import Naukri
from applicant.errors import Blocked, NotFound
from applicant.financials import CrunchbaseClient, TracxnClient
from applicant.reviews import GlassdoorClient
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
        self.assertFalse(any('Submit' in url for url in self.requested), 'nothing was submitted')


if __name__ == '__main__':
    unittest.main()
