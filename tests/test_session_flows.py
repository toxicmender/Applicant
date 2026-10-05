"""Glassdoor and LinkedIn's signed-in flows, on a scripted page (tests/fakes.py).

Both keep a browser session: Glassdoor pages through reviews in a persistent
profile and saves the session after a --login run; LinkedIn signs in, restores
a saved session, scrapes recommended jobs and opens Easy Apply forms. Until
now only their parsing and the confirmation step were tested.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from applicant.boards.linkedin_apply import LinkedIn
from applicant.errors import Blocked, SourceError
from applicant.infra.browser import BrowserSession
from applicant.interaction import Scripted
from applicant.reviews import GlassdoorClient
from tests.fakes import Element, FakePage, FakeResponse, Session, Visit, patched_browser


def reviews(start: int, count: int) -> list[dict]:
    return [
        {'pros': f'pro {n}', 'cons': 'c', 'summary': f'review {n}'}
        for n in range(start, start + count)
    ]


def graph(*items: dict, **employer) -> FakeResponse:
    payload: dict = {'reviews': list(items)}
    if employer:
        payload['employer'] = employer
    return FakeResponse(url='https://www.glassdoor.com/graph', payload=payload)


def apollo_html(state: dict) -> str:
    return '<script>window.appCache={"apolloState":' + json.dumps(state) + '};</script>'


class GlassdoorFetchTest(unittest.TestCase):
    def fetch(self, site, company='Google-E9079', max_reviews=15, **options):
        page = FakePage(site)
        client = GlassdoorClient(**options)
        with patched_browser(page) as session, self.assertLogs('applicant', 'INFO'):
            rating = client.fetch(company, max_reviews=max_reviews)
        return rating, page, session

    def test_pages_until_enough_reviews_from_its_api_calls_and_its_html(self):
        site = [
            (
                r'E9079\.htm$',
                Visit(responses=[graph(*reviews(0, 10), ratingOverall=4.4, reviewCount=900)]),
            ),
            (r'_P2\.htm$', Visit(html=apollo_html({'ROOT_QUERY': {'reviews': reviews(10, 10)}}))),
        ]
        rating, page, session = self.fetch(site, max_reviews=15)
        self.assertEqual(len(rating.reviews), 15, 'cut to what was asked for')
        self.assertEqual((rating.overall_rating, rating.review_count), (4.4, 900))
        self.assertEqual(len(page.visited), 2)
        self.assertEqual(session.options[0]['profile_dir'], '.gd_profile')
        self.assertEqual(session.saved, [], 'only a --login run saves the session')

    def test_a_missing_page_ends_the_paging(self):
        site = [
            (r'E9079\.htm$', Visit(responses=[graph(*reviews(0, 10))])),
            (r'_P2', Visit(status=404)),
        ]
        rating, page, _ = self.fetch(site, max_reviews=30)
        self.assertEqual((len(rating.reviews), len(page.visited)), (10, 2))

    def test_paging_gives_up_after_thirty_pages(self):
        site = [(r'glassdoor', Visit(responses=[graph(*reviews(0, 1))]))]
        _, page, _ = self.fetch(site, max_reviews=100)
        self.assertEqual(len(page.visited), 30)

    def test_a_login_run_pauses_once_and_saves_the_session(self):
        said = Scripted()
        site = [(r'glassdoor', Visit(responses=[graph(*reviews(0, 10))]))]
        _, _, session = self.fetch(site, max_reviews=10, login=True, interaction=said)
        self.assertEqual(len(said.said), 1)
        self.assertEqual(session.saved, [None], 'saved beside its profile')

    def test_a_session_that_cannot_be_saved_is_reported_not_fatal(self):
        site = [(r'glassdoor', Visit(responses=[graph(*reviews(0, 10))]))]
        failing = mock.patch.object(BrowserSession, 'save_state', side_effect=OSError('read-only'))
        with (
            patched_browser(FakePage(site)),
            failing,  # after patched_browser, so it wins
            self.assertLogs('applicant', 'WARNING') as logged,
        ):
            client = GlassdoorClient(login=True, interaction=Scripted())
            rating = client.fetch('Google-E9079', max_reviews=10)
        self.assertEqual(len(rating.reviews), 10)
        self.assertIn('could not save the session state', '\n'.join(logged.output))

    def test_a_bot_check_is_blocked(self):
        with (
            self.assertRaisesRegex(Blocked, '--login'),
            patched_browser(FakePage([(r'.', Visit(blocked=True))])),
        ):
            GlassdoorClient().fetch('Google-E9079')

    def test_a_browser_failure_is_this_source_failing_in_one_line(self):
        crashed = FakePage([(r'.', Visit(raises=RuntimeError('Target closed\nCall log: ...')))])
        with (
            patched_browser(crashed),
            self.assertRaisesRegex(
                SourceError, r'browser session failed: RuntimeError: Target closed$'
            ),
        ):
            GlassdoorClient().fetch('Google-E9079')

    def test_only_its_own_graph_calls_are_read(self):
        heard = [
            FakeResponse(
                url='https://www.glassdoor.com/static/app.json', payload={'reviews': reviews(0, 5)}
            ),
            FakeResponse(url='https://www.glassdoor.com/graph', payload=ValueError('html')),
            FakeResponse(url='https://www.glassdoor.com/graph', payload={}),
            graph(*reviews(0, 2)),
        ]
        rating, *_ = self.fetch(
            [(r'E9079\.htm$', Visit(responses=heard)), (r'_P', Visit(status=404))]
        )
        self.assertEqual(len(rating.reviews), 2)

    def test_the_embedded_state_in_either_form(self):
        client = GlassdoorClient()
        html = (
            apollo_html({'r': reviews(0, 1)})
            + '<script>x={"apolloState":{broken};</script>'
            + '<script id="__NEXT_DATA__" type="application/json">{"props": 1}</script>'
        )
        self.assertEqual(client._embedded(html), [{'r': reviews(0, 1)}, {'props': 1}])
        self.assertEqual(client._embedded('<script id="__NEXT_DATA__">{oops</script>'), [])

    def test_a_name_alone_cannot_be_looked_up(self):
        with self.assertRaisesRegex(SourceError, 'reviews url'):
            GlassdoorClient().fetch('Google')


class LinkedInSignInTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'cookies.json'

    def sign_in(self, site, *answers, **options):
        page = FakePage(site)
        client = LinkedIn(delay=0, interaction=Scripted(*answers))
        self.addCleanup(client.close)
        with patched_browser(page) as session:
            saved = client.login('me@example.com', 'secret', filepath=self.path, **options)
        return saved, page, session

    def test_a_saved_session_is_not_replaced_unless_asked(self):
        self.path.write_text('{}', encoding='utf-8')
        with self.assertLogs('applicant', 'WARNING'):
            saved, page, _ = self.sign_in([])
        self.assertIsNone(saved)
        self.assertEqual(page.visited, [], 'no browser opened')

    def test_signing_in_saves_the_session(self):
        site = [(r'/login$', Visit(redirect='https://www.linkedin.com/feed/'))]
        with self.assertLogs('applicant', 'INFO'):
            saved, page, session = self.sign_in(site)
        self.assertEqual(saved, str(self.path))
        self.assertEqual(session.saved, [self.path])
        self.assertEqual(page.filled, {'#username': 'me@example.com', '#password': 'secret'})

    def test_a_one_time_code_is_asked_for_and_entered(self):
        pin = 'input[name="pin"], #input__phone_verification_pin'
        site = [
            (
                r'/login$',
                Visit(redirect='https://www.linkedin.com/feed/', elements={pin: [Element()]}),
            )
        ]
        with self.assertLogs('applicant', 'INFO'):
            saved, page, _ = self.sign_in(site, '123456', twoFA=True)
        self.assertEqual(page.filled[pin], '123456')
        self.assertIsNotNone(saved)

    def test_a_sign_in_that_stops_at_a_wall_is_blocked(self):
        for wall in (
            'https://www.linkedin.com/login?fail=1',
            'https://www.linkedin.com/checkpoint/challenge',
        ):
            with self.subTest(wall=wall):
                site = [(r'/login$', Visit(redirect=wall))]
                with self.assertLogs('applicant', 'WARNING'), self.assertRaises(Blocked):
                    self.sign_in(site, overwrite=True)


class LinkedInSessionTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'cookies.json'
        self.client = LinkedIn(delay=0)
        self.addCleanup(self.client.close)

    def restore(self, site) -> tuple[bool, Session]:
        with patched_browser(FakePage(site)) as session:
            restored = self.client.restore_session(self.path)
        return restored, session

    def test_unusable_files_are_no_session(self):
        for content, reason in (
            (None, 'no usable session'),
            ('{not json', 'not valid JSON'),
            ('{"list": [{"name": "li_at"}]}', 'Selenium'),
        ):
            with self.subTest(reason=reason):
                if content is not None:
                    self.path.write_text(content, encoding='utf-8')
                with self.assertLogs('applicant', 'WARNING') as logged:
                    restored, session = self.restore([])
                self.assertFalse(restored)
                self.assertEqual(session.started, 0, 'no browser for a file that is no session')
                self.assertIn(reason, '\n'.join(logged.output))

    def test_an_expired_session_is_reported(self):
        self.path.write_text('{"cookies": [], "origins": []}', encoding='utf-8')
        site = [(r'/feed/', Visit(redirect='https://www.linkedin.com/login?session_redirect=feed'))]
        with self.assertLogs('applicant', 'WARNING') as logged:
            restored, _ = self.restore(site)
        self.assertFalse(restored)
        self.assertIn('has expired', '\n'.join(logged.output))

    def test_a_live_session_is_restored_into_the_browser(self):
        state = {'cookies': [{'name': 'li_at'}], 'origins': []}
        self.path.write_text(json.dumps(state), encoding='utf-8')
        with self.assertLogs('applicant', 'INFO'):
            restored, session = self.restore([(r'/feed/', Visit())])
        self.assertTrue(restored)
        self.assertEqual(session.options[0]['storage_state'], state)


class LinkedInPagesTest(unittest.TestCase):
    def setUp(self):
        self.client = LinkedIn(delay=0)
        self.addCleanup(self.client.close)

    def test_scraping_signed_out_is_blocked(self):
        site = [(r'recommended', Visit(redirect='https://www.linkedin.com/authwall?trk=x'))]
        with patched_browser(FakePage(site)), self.assertRaisesRegex(Blocked, 'not signed in'):
            self.client.scrape_jobs()

    def test_recommended_jobs_are_read_off_the_cards(self):
        cards = '[data-job-id], .job-card-container'
        link = 'a[href*="/jobs/view/"]'
        found = [
            Element(
                text='ML Engineer\nAcme\nPune\nEasy Apply',
                attrs={'data-job-id': '7'},
                children={link: [Element(attrs={'href': '/jobs/view/7/?trk=x'})]},
            ),
            Element(text='   '),  # an empty card
            Element(raises=RuntimeError('scrolled out')),
            Element(text='Data Scientist', attrs={'data-job-id': '8'}),
        ]
        page = FakePage([(r'recommended', Visit(elements={cards: found}))])
        with (
            tempfile.TemporaryDirectory() as folder,
            patched_browser(page),
            self.assertLogs('applicant', 'INFO'),
        ):
            jobs = self.client.scrape_jobs(str(Path(folder) / 'jobs.json'))
        self.assertEqual([job.id for job in jobs], ['7', '8'])
        self.assertEqual(jobs[0].url, 'https://www.linkedin.com/jobs/view/7/')
        self.assertTrue(jobs[0].easy_apply)
        self.assertEqual((jobs[1].company, jobs[1].url, jobs[1].easy_apply), (None, None, False))
        self.assertEqual(page.mouse.wheel.call_count, 1, 'stops once the list stops growing')

    def test_a_multi_step_form_is_closed_and_left_alone(self):
        follow = Element(checked=True)
        visit = Visit(
            elements={'button.jobs-apply-button': [Element()], '#follow-company-checkbox': [follow]}
        )
        page = FakePage([(r'jobs/view', visit)])
        posting = {'url': 'https://www.linkedin.com/jobs/view/9', 'title': 'ML'}
        with self.assertLogs('applicant', 'INFO'):
            outcome = self.client._apply_one(page, posting)
        self.assertIs(outcome, False)
        self.assertEqual(page.pressed, ['Escape'])
        self.assertFalse(follow.checked, 'following the company is unticked first')

    def test_the_session_is_started_once_and_closed_once(self):
        with patched_browser(FakePage()) as session:
            self.assertIs(self.client._session(), self.client._session())
            self.client.close()
            self.client.close()
        self.assertEqual(session.closed, 1)


if __name__ == '__main__':
    unittest.main()
