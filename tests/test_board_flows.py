"""The boards' browser flows, run against a scripted page (tests/fakes.py).

Naukri and Google Jobs only exist in a browser, and Indeed falls back to one;
until now only their parsing was tested. Here their own search loops run:
paging, limits, dedupe, the API-or-DOM choice, lazy loading, bot checks.
What this cannot show is that the selectors match the live sites - see
tests/test_live_pages.py for the same flows in a real browser.
"""

from __future__ import annotations

import unittest

import httpx

from applicant.boards.googlejobs import CARD, GoogleJobs
from applicant.boards.indeed import Indeed
from applicant.boards.naukri import BASE, Naukri
from applicant.errors import Blocked
from tests.fakes import Element, FakePage, FakeResponse, Visit, patched_browser
from tests.test_boards import mosaic_html

TUPLE = '.srp-jobtuple-wrapper'


def api(*ids: int, url: str = BASE + '/jobapi/v3/search?k=python') -> FakeResponse:
    return FakeResponse(
        url=url,
        payload={
            'jobDetails': [
                {
                    'jobId': str(n),
                    'title': f'Python Dev {n}',
                    'companyName': 'Acme',
                    'jdURL': f'/job-listings-python-dev-{n}',
                    'placeholders': [{'type': 'location', 'label': 'Pune'}],
                }
                for n in ids
            ]
        },
    )


def page_of(*responses: FakeResponse, tuples: int = 1, **extra) -> Visit:
    return Visit(
        elements={TUPLE: [Element() for _ in range(tuples)]}, responses=list(responses), **extra
    )


def card(title: str | None, href: str = '', **fields: str) -> Element:
    children = {f'.{name}': [Element(text=value)] for name, value in fields.items()}
    if title is not None:
        children['a.title'] = [Element(text=title, attrs={'href': href})]
    return Element(children=children)


class NaukriSearchTest(unittest.TestCase):
    def search(self, site, **kwargs):
        page = FakePage(site)
        with patched_browser(page) as session:
            jobs = Naukri(delay=0).search('python', **kwargs)
        self.assertEqual(session.closed, 1, 'the browser is closed with the search')
        return jobs, page

    def test_its_own_api_call_is_read_page_after_page_until_the_limit(self):
        site = [
            (r'python-jobs$', page_of(api(1, 2))),
            (r'python-jobs-2$', page_of(api(2, 3))),  # 2 again: one copy kept
            (r'python-jobs-3$', page_of(api(4))),
        ]
        jobs, page = self.search(site, limit=3)
        self.assertEqual([job.id for job in jobs], ['1', '2', '3'])
        self.assertEqual(len(page.visited), 2, 'no page fetched past the limit')
        self.assertEqual(jobs[0].url, BASE + '/job-listings-python-dev-1')
        self.assertEqual(jobs[0].location, 'Pune')

    def test_with_no_api_call_the_rendered_tuples_are_read(self):
        tuples = [
            card(
                'Python Developer',
                BASE + '/job-listings-python-developer-acme-1234567?src=jobsearch',
                **{'comp-name': 'Acme', 'locWdth': 'Pune', 'sal': 'Not disclosed'},
            ),
            card(None),  # no title link: skipped
            Element(
                raises=RuntimeError('detached'),
                children={'a.title': [Element(raises=RuntimeError('detached'))]},
            ),
        ]
        site = [(r'python-jobs$', Visit(elements={TUPLE: tuples}))]
        jobs, _ = self.search(site, limit=10)
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.id, '1234567', 'taken from the url when the card has none')
        self.assertEqual(job.url, BASE + '/job-listings-python-developer-acme-1234567')
        self.assertIsNone(job.salary, '"Not disclosed" is no salary')
        self.assertEqual((job.company, job.location), ('Acme', 'Pune'))

    def test_a_field_that_vanishes_mid_read_is_just_missing(self):
        tuple_ = card('Dev', BASE + '/x-7654321')
        tuple_.children['.comp-name'] = [Element(raises=RuntimeError('gone'))]
        jobs, _ = self.search([(r'python-jobs$', Visit(elements={TUPLE: [tuple_]}))], limit=1)
        self.assertIsNone(jobs[0].company)

    def test_the_card_id_wins_over_the_url(self):
        tuple_ = card('Dev', BASE + '/x-7654321')
        tuple_.attrs['data-job-id'] = '42'
        jobs, _ = self.search([(r'python-jobs$', Visit(elements={TUPLE: [tuple_]}))], limit=1)
        self.assertEqual(jobs[0].id, '42')

    def test_responses_that_are_not_its_search_are_ignored(self):
        noise = [
            FakeResponse(
                url=BASE + '/jobapi/v3/search', status=406, payload={'jobDetails': [{'title': 'x'}]}
            ),
            FakeResponse(url=BASE + '/jobapi/v3/search', payload=ValueError('not json')),
            FakeResponse(url=BASE + '/other', payload={'jobDetails': [{'title': 'x'}]}),
        ]
        jobs, _ = self.search([(r'python-jobs$', page_of(*noise, api(9)))], limit=5)
        self.assertEqual([job.id for job in jobs], ['9'])

    def test_a_page_without_tuples_ends_the_search_quietly(self):
        site = [(r'python-jobs$', page_of(api(1))), (r'python-jobs-2$', Visit())]
        jobs, page = self.search(site, limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(page.visited), 2)

    def test_tuples_with_nothing_readable_end_it_too(self):
        site = [(r'python-jobs$', Visit(elements={TUPLE: [card(None)]}))]
        jobs, _ = self.search(site, limit=10)
        self.assertEqual(jobs, [])

    def test_a_bot_check_is_blocked(self):
        with self.assertRaises(Blocked):
            self.search([(r'python-jobs$', page_of(api(1), blocked=True))])

    def test_a_location_goes_into_the_url(self):
        _, page = self.search([(r'.', Visit())], location='New Delhi')
        self.assertEqual(page.visited, [BASE + '/python-jobs-in-new-delhi'])


def google_card(title: str, company: str = 'Acme', place: str = 'Pune • via LinkedIn') -> Element:
    return Element(text=f'A\n{title}\n{company}\n{place}\n3 days ago\nFull-time')


class GoogleJobsSearchTest(unittest.TestCase):
    def search(self, visit: Visit, limit: int = 20):
        page = FakePage([(r'google\.com/search', visit)])
        with patched_browser(page):
            jobs = GoogleJobs().search('python', 'Pune', limit=limit)
        return jobs, page

    def cards(self, start: int, count: int) -> list[Element]:
        return [google_card(f'Role {n}') for n in range(start, start + count)]

    def test_enough_cards_up_front_means_no_scrolling(self):
        jobs, page = self.search(Visit(elements={CARD: self.cards(0, 25)}), limit=20)
        self.assertEqual(len(jobs), 20)
        page.mouse.wheel.assert_not_called()
        self.assertEqual(jobs[0].via, 'LinkedIn')

    def test_scrolling_stops_after_three_stalls_in_a_row(self):
        visit = Visit(
            elements={CARD: self.cards(0, 10)}, more=[{}, {}, {}, {CARD: self.cards(10, 10)}]
        )
        jobs, page = self.search(visit, limit=30)
        self.assertEqual(len(jobs), 10)
        self.assertEqual(page.mouse.wheel.call_count, 3)

    def test_a_scroll_that_loads_more_resets_the_stall_count(self):
        visit = Visit(
            elements={CARD: self.cards(0, 10)},
            more=[{}, {CARD: self.cards(10, 10)}, {}, {}, {}],
        )
        jobs, page = self.search(visit, limit=30)
        self.assertEqual(len(jobs), 20)
        self.assertEqual(page.mouse.wheel.call_count, 5)

    def test_duplicates_odd_cards_and_detached_ones_are_skipped(self):
        cards = [
            google_card('Role'),
            google_card('Role'),  # the same job twice
            Element(text='X'),  # a logo initial and nothing else
            Element(raises=RuntimeError('detached')),
            google_card('Other'),
        ]
        jobs, _ = self.search(Visit(elements={CARD: cards}), limit=5)
        self.assertEqual([job.title for job in jobs], ['Role', 'Other'])

    def test_a_bot_check_is_blocked(self):
        with self.assertRaises(Blocked):
            self.search(Visit(elements={CARD: self.cards(0, 3)}, blocked=True))

    def test_no_cards_at_all_is_blocked_with_the_reason(self):
        with self.assertRaisesRegex(Blocked, 'no job cards'):
            self.search(Visit())


class IndeedBrowserFallbackTest(unittest.TestCase):
    """A refused plain request is retried in a browser."""

    def search(self, handler, visit: Visit):
        client = Indeed(
            domain='https://www.indeed.com',
            delay=0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        page = FakePage([(r'indeed\.com/jobs', visit)])
        with patched_browser(page):
            return client.search('python', 'Pune', limit=2)

    def test_a_403_is_retried_in_the_browser(self):
        visit = Visit(
            html=mosaic_html([{'jobkey': 'k1', 'title': 'Dev'}, {'jobkey': 'k2', 'title': 'Ops'}])
        )
        with self.assertLogs('applicant.boards.indeed', 'INFO'):
            jobs = self.search(lambda request: httpx.Response(403, text='denied'), visit)
        self.assertEqual([job.id for job in jobs], ['k1', 'k2'])

    def test_an_unreachable_network_is_retried_in_the_browser_too(self):
        def offline(request):
            raise httpx.ConnectError('down', request=request)

        visit = Visit(html=mosaic_html([{'jobkey': 'k1', 'title': 'Dev'}]))
        with self.assertLogs('applicant', 'INFO'):
            jobs = self.search(offline, visit)
        self.assertEqual([job.id for job in jobs], ['k1'])

    def test_a_bot_check_in_the_browser_is_blocked(self):
        with self.assertLogs('applicant', 'INFO'), self.assertRaises(Blocked):
            self.search(lambda request: httpx.Response(403), Visit(blocked=True))


if __name__ == '__main__':
    unittest.main()
