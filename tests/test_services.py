"""applicant.services, the events they emit, and the CLI's use of them.

The rule this phase is judged by: the library never prints. Only the CLI
package does, and only `applicant.interaction` reads from the keyboard.
"""

from __future__ import annotations

import ast
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from applicant.boards import Capability
from applicant.cli import main
from applicant.domain.job import Job
from applicant.errors import AuthFailed, Blocked, NotFound, QuotaExhausted, Unparseable
from applicant.filters import JobFilter
from applicant.financials import CompanyFinancials, CrunchbaseClient, Money, TracxnClient
from applicant.interaction import Scripted
from applicant.reviews import GlassdoorClient
from applicant.reviews.models import CompanyRating
from applicant.search import Jobs
from applicant.services import financials as financials_service
from applicant.services import rates as rates_service
from applicant.services.events import (
    BoardSearched,
    FactorFetched,
    FinancialsTracked,
    RatingFetched,
    SourceFailed,
)
from applicant.services.fanout import fan_out
from applicant.services.reviews import fetch_reviews
from applicant.services.search import SearchJobs
from applicant.services.status import summarise
from applicant.storage import ApplicationLog, save_jobs

PACKAGE = Path(__file__).resolve().parent.parent / 'src' / 'applicant'


def calls_to(name: str) -> dict[str, int]:
    """Module (relative to the package) -> how many times it calls `name()`."""
    found = {}
    for path in sorted(PACKAGE.rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == name
        )
        if count:
            found[str(path.relative_to(PACKAGE))] = count
    return found


class NoPrintingTest(unittest.TestCase):
    def test_only_the_cli_prints(self):
        outside = {module for module in calls_to('print') if not module.startswith('cli/')}
        self.assertEqual(outside, set(), 'the library logs; only commands print')

    def test_only_the_interaction_module_reads_the_keyboard(self):
        self.assertEqual(set(calls_to('input')), {'interaction.py'})


class Collect(list):
    """An `emit` that keeps every event, for a test to read back."""

    def __call__(self, event):
        self.append(event)


class FanOutTest(unittest.TestCase):
    def test_a_failing_source_costs_only_itself(self):
        def call(name):
            if name == 'b':
                raise Blocked('bot check')
            if name == 'c':
                raise RuntimeError('reshaped')
            return name.upper()

        events = Collect()
        with self.assertLogs('applicant.services.fanout', 'WARNING'):
            outcomes = fan_out(['a', 'b', 'c', 'd'], call, events, subject='zomato')

        self.assertEqual([outcome.result for outcome in outcomes], ['A', None, None, 'D'])
        self.assertEqual(
            [(e.source, e.expected, e.subject) for e in events],
            [
                ('b', True, 'zomato'),
                ('c', False, 'zomato'),
            ],
        )

    def test_being_interrupted_is_not_a_source_failing(self):
        def call(name):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            fan_out(['a'], call)


class FakeBoard:
    capability = Capability()

    def __init__(self, jobs):
        self.jobs = jobs
        self.closed = False

    def search(self, *args, **kwargs):
        return self.jobs

    def close(self):
        self.closed = True


class SearchServiceTest(unittest.TestCase):
    def test_a_board_that_cannot_even_be_made_is_isolated(self):
        """Making the client used to sit outside the isolation; now it is inside."""
        good = FakeBoard([Job(source='indeed', id='1', title='Dev')])

        def client_for(name):
            if name == 'naukri':
                raise RuntimeError('no browser for you')
            return good

        events = Collect()
        with self.assertLogs('applicant.services.fanout', 'ERROR'):
            found = SearchJobs(client_for).run(
                ['naukri', 'indeed'], 'dev', JobFilter(), emit=events
            )

        self.assertEqual([job.id for job in found], ['1'])
        self.assertEqual([type(event) for event in events], [SourceFailed, BoardSearched], events)

    def test_boards_kept_open_are_not_closed(self):
        board = FakeBoard([])
        SearchJobs(lambda name: board, keep_open={'linkedin'}).run(['linkedin'], 'x', JobFilter())
        self.assertFalse(board.closed)
        SearchJobs(lambda name: board).run(['indeed'], 'x', JobFilter())
        self.assertTrue(board.closed)

    def test_the_facade_still_reports_through_on_error(self):
        heard = []
        facade = Jobs(sources=['indeed'])
        broken = FakeBoard([])
        broken.search = mock.Mock(side_effect=Blocked('bot check'))
        with (
            mock.patch.object(Jobs, '_client', lambda self, name: broken),
            self.assertLogs('applicant.services.fanout', 'WARNING'),
        ):
            facade.search('x', on_error=lambda name, error: heard.append((name, str(error))))
        self.assertEqual(heard, [('indeed', 'bot check')])


class TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)


def rating(source: str) -> CompanyRating:
    return CompanyRating(source=source, company='TCS', url='https://x', overall_rating=3.8)


class ReviewsServiceTest(TempDir):
    def client(self, result):
        client = mock.Mock()
        client.fetch.side_effect = result if isinstance(result, Exception) else None
        client.fetch.return_value = result
        return client

    def test_one_rating_is_written_as_an_object_several_as_a_list(self):
        output = str(self.root / 'reviews.json')
        fetch_reviews('tcs', {'ambitionbox': self.client(rating('ambitionbox'))}, output)
        self.assertIsInstance(json.loads(Path(output).read_text()), dict)

        fetch_reviews(
            'tcs',
            {
                'ambitionbox': self.client(rating('ambitionbox')),
                'glassdoor': self.client(rating('glassdoor')),
            },
            output,
        )
        self.assertEqual(len(json.loads(Path(output).read_text())), 2)

    def test_each_rating_is_emitted_as_it_arrives(self):
        events = Collect()
        fetch_reviews(
            'tcs',
            {'ambitionbox': self.client(rating('ambitionbox'))},
            str(self.root / 'r.json'),
            emit=events,
        )
        self.assertEqual([type(event) for event in events], [RatingFetched])

    def test_nothing_fetched_writes_nothing(self):
        output = self.root / 'reviews.json'
        with self.assertLogs('applicant', 'WARNING'):
            found = fetch_reviews(
                'tcs', {'glassdoor': self.client(Blocked('cloudflare'))}, str(output)
            )
        self.assertIsNone(found.written_to)
        self.assertFalse(output.exists())


class FinancialsServiceTest(TempDir):
    def financials(self, source, company):
        return CompanyFinancials(
            source=source,
            company=company,
            total_funding=Money(amount=1e9, currency='USD', amount_usd=1e9),
        )

    def test_a_profile_url_only_goes_to_the_site_it_belongs_to(self):
        self.assertTrue(CrunchbaseClient(api_key='').accepts('zomato'))
        self.assertFalse(
            CrunchbaseClient(api_key='').accepts('https://tracxn.com/d/companies/z/__1')
        )
        self.assertFalse(
            TracxnClient(api_key='').accepts('https://www.crunchbase.com/organization/z')
        )

    def test_an_unexpected_error_no_longer_ends_the_run(self):
        """Only SourceError was caught before; anything else crashed every company."""
        broken, working = mock.Mock(), mock.Mock()
        broken.fetch.side_effect = RuntimeError('page crashed')
        working.fetch.side_effect = lambda company, max_rounds: self.financials('tracxn', company)
        del broken.accepts, working.accepts  # a client without the method accepts everything

        events = Collect()
        with self.assertLogs('applicant.services.fanout', 'ERROR'):
            tracked = financials_service.track_financials(
                ['zomato'],
                {'crunchbase': broken, 'tracxn': working},
                str(self.root / 'history.json'),
                pause=0,
                emit=events,
            )
        self.assertEqual(tracked.found, 1)
        self.assertEqual([type(event) for event in events], [SourceFailed, FinancialsTracked])

    def test_companies_come_from_the_listing_once_each(self):
        listing = str(self.root / 'jobs.json')
        save_jobs(
            [
                Job(source='indeed', id='1', title='Dev', company='Zomato'),
                Job(source='naukri', id='2', title='Dev', company='zomato'),
            ],
            listing,
        )
        self.assertEqual(
            financials_service.companies_to_track(['Swiggy'], listing), ['Swiggy', 'Zomato']
        )


class RatesServiceTest(unittest.TestCase):
    def test_each_factor_is_emitted_as_it_arrives(self):
        def fake_refresh(path=None, currencies=None, force=False, on_result=None):
            assert on_result is not None
            on_result('GBR', {'value': 0.7, 'year': '2025'}, False)
            on_result('USA', {'value': 1.0, 'year': 'definition'}, True)
            return ['GBR'], [], ['USA']

        events = Collect()
        with mock.patch.object(rates_service, 'refresh_factors', fake_refresh):
            done = rates_service.refresh(['GBP'], emit=events)
        self.assertEqual(done.updated, ['GBR'])
        self.assertEqual(
            events,
            [
                FactorFetched('GBR', {'value': 0.7, 'year': '2025'}, False),
                FactorFetched('USA', {'value': 1.0, 'year': 'definition'}, True),
            ],
        )

    def test_the_table_lists_every_mapped_currency(self):
        table = rates_service.cached()
        usd = next(row for row in table.rows if row.currency == 'USD')
        self.assertEqual((usd.value, usd.year), (1.0, 'definition'))
        self.assertLessEqual(table.known, table.countries)


class StatusServiceTest(TempDir):
    def test_counts_by_source_and_status(self):
        listing, log = str(self.root / 'jobs.json'), str(self.root / 'applied.csv')
        jobs = [Job(source='indeed', id='1', title='A'), Job(source='naukri', id='2', title='B')]
        save_jobs(jobs, listing)
        ApplicationLog(log).record([(jobs[0], 'applied', '')])
        status = summarise(listing, log)
        self.assertEqual((status.jobs, status.applications), (2, 1))
        self.assertEqual(status.to_dict()['applications']['by_status'], {'applied': 1})


class InteractionTest(unittest.TestCase):
    def test_a_login_pause_asks_the_interaction_and_prints_nothing(self):
        page = mock.Mock()
        asked = Scripted()
        client = GlassdoorClient(login=True, interaction=asked)
        out = io.StringIO()
        with redirect_stdout(out):
            client._settle(page)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('Clear the Cloudflare check', asked.said[0])

    def test_scripted_answers_run_out_loudly(self):
        with self.assertRaises(RuntimeError):
            Scripted().ask('Enter OTP: ')


class ExitCodeTest(unittest.TestCase):
    def run_main(self, error: BaseException) -> int:
        with (
            mock.patch('applicant.cli.run_status', side_effect=error),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            return main(['status', '--no-log-file'])

    def test_a_source_failure_that_ends_the_run_says_what_kind(self):
        for error, code in (
            (Blocked('bot check'), 3),
            (NotFound('no such company'), 4),
            (Unparseable('layout changed'), 5),
            (AuthFailed('key refused'), 6),
            (QuotaExhausted('out of credits'), 6),
            (RuntimeError('a bug'), 1),
        ):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(self.run_main(error), code)


if __name__ == '__main__':
    unittest.main()
