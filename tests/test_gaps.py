"""The branches the rest of the suite passed by: rendering, the facade's
LinkedIn client, settings from the environment and a bad config file, the
store's own failures, PPP factors that cannot be refreshed, the launcher's
edges, the apply service's failures, and AmbitionBox's transport errors.
"""

from __future__ import annotations

import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import httpx

from applicant import log, money
from applicant.cli import main
from applicant.cli.render import Renderer
from applicant.domain.job import Job
from applicant.domain.ports import ApplicationResult
from applicant.errors import Blocked, ConfigError, SourceError, StoreError, Unparseable
from applicant.financials.models import CompanyFinancials, FundingRound, Money
from applicant.infra.browser import BrowserSession
from applicant.infra.store.sqlite import Store
from applicant.reviews import AmbitionBoxClient
from applicant.reviews.models import CompanyRating
from applicant.search import Jobs
from applicant.services.apply import ApplyToJobs, easy_apply_with
from applicant.services.events import FactorFetched, FinancialsTracked, SourceFailed
from applicant.services.reviews import Reviews, fetch_reviews
from applicant.settings import Settings
from applicant.storage import ApplicationLog
from tests.test_reviews import ld_json_html


class TempDir(unittest.TestCase):
    def setUp(self):
        # main() configures logging for the process; leave it as found
        self.addCleanup(log.silence)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)


def printed(*events, **options) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        render = Renderer(**options)
        for event in events:
            render(event)
    return out.getvalue()


class RenderTest(unittest.TestCase):
    def test_financials_with_changes_notes_and_rounds(self):
        financials = CompanyFinancials(
            source='tracxn',
            company='Zomato',
            notes=['revenue: not in your plan'],
            rounds=[
                FundingRound(
                    date='2021-02-17', round='Series J', amount=Money(amount=2.5e8, currency='USD')
                )
            ],
        )
        event = FinancialsTracked('tracxn', financials, ['total funding: $2.1B -> $2.35B'])
        shown = printed(event, rounds=True)
        self.assertIn('  changed: total funding: $2.1B -> $2.35B', shown)
        self.assertIn('  note: revenue: not in your plan', shown)
        self.assertIn('Series J', shown)
        self.assertNotIn('Series J', printed(event), 'rounds only when asked for')

    def test_failures_only_when_asked_with_or_without_a_subject(self):
        about = SourceFailed('crunchbase', Blocked('bot check'), subject='zomato')
        bare = SourceFailed('crunchbase', Blocked('bot check'))
        self.assertEqual(printed(about), '')
        self.assertEqual(
            printed(about, bare, failures=True),
            'crunchbase: zomato: bot check\ncrunchbase: bot check\n',
        )

    def test_factors(self):
        shown = printed(
            FactorFetched('SWE', {'value': 10.5, 'year': '2024'}),
            FactorFetched('NOR', None),
            FactorFetched('USA', {'value': 1.0, 'year': '-'}, skipped=True),
        )
        self.assertEqual(shown, '  SWE: 10.5 (2024)\n  NOR: no value returned\n')

    def test_scraped_text_cannot_rewrite_the_terminal(self):
        financials = CompanyFinancials(source='x', company='Evil\x1b[2J\rCo')
        self.assertNotIn('\x1b', printed(FinancialsTracked('x', financials)))


class ReviewsTest(TempDir):
    def rating(self) -> CompanyRating:
        return CompanyRating(source='ambitionbox', company='TCS', url='u', overall_rating=3.8)

    def test_the_sqlite_store_keeps_each_rating_as_history(self):
        client = mock.Mock()
        client.fetch.return_value = self.rating()
        output = str(self.root / 'reviews.json')
        with self.assertLogs('applicant', 'INFO'):
            fetch_reviews('tcs', {'ambitionbox': client}, output, backend='sqlite')
            fetch_reviews('tcs', {'ambitionbox': client}, output, backend='sqlite')
        with Store.beside(output) as store:
            self.assertEqual(len(store.rating_history('TCS')), 2)

    def run_cli(self, found: Reviews) -> tuple[int, str]:
        out = io.StringIO()
        with (
            mock.patch('applicant.services.reviews.fetch_reviews', return_value=found),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = main(['reviews', 'tcs', '--data-dir', str(self.root), '--no-log-file'])
        return code, out.getvalue()

    def test_the_command_says_where_it_wrote(self):
        code, out = self.run_cli(Reviews([self.rating()], str(self.root / 'reviews.json')))
        self.assertEqual(code, 0)
        self.assertIn('written to', out)

    def test_nothing_written_exits_1(self):
        self.assertEqual(self.run_cli(Reviews([], None))[0], 1)


class FacadeLinkedInTest(unittest.TestCase):
    def test_a_restore_that_raises_closes_the_client_it_made(self):
        from applicant.boards.linkedin_apply import LinkedIn

        with (
            mock.patch.object(LinkedIn, 'restore_session', side_effect=KeyboardInterrupt),
            mock.patch.object(LinkedIn, 'close') as closed,
            self.assertRaises(KeyboardInterrupt),
        ):
            Jobs().linkedin()
        closed.assert_called_once()

    def test_a_client_handed_in_is_the_one_searched_with(self):
        handed = mock.Mock()
        self.assertIs(Jobs(linkedin=handed)._client('linkedin'), handed)


class SettingsTest(TempDir):
    def test_the_money_cache_can_come_from_the_environment(self):
        settings = Settings.load(
            environ={'APPLICANT_MONEY_CACHE': str(self.root / 'fx.json')}, data_dir=self.root
        )
        self.assertEqual(settings.files.money_cache, str(self.root / 'fx.json'))

    def test_a_config_file_named_but_missing_is_an_error(self):
        with self.assertRaisesRegex(ConfigError, 'does not exist'):
            Settings.load(config=self.root / 'nope.toml', environ={})

    def test_a_config_file_that_is_not_toml_is_an_error(self):
        broken = self.root / 'applicant.toml'
        broken.write_text('data_dir = [unclosed', encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'could not read'):
            Settings.load(config=broken, environ={})


class StoreEdgesTest(TempDir):
    def setUp(self):
        super().setUp()
        self.store = Store.beside(self.root / 'job_listing.json')
        self.addCleanup(self.store.close)

    def test_a_database_error_is_a_store_error(self):
        with self.assertRaisesRegex(StoreError, 'disk I/O'), self.store._transaction():
            raise sqlite3.OperationalError('disk I/O error')

    def test_a_file_outside_its_directory_keeps_its_full_name(self):
        elsewhere = Path(tempfile.gettempdir()).resolve() / 'other-listing.json'
        self.assertEqual(self.store.name(elsewhere), elsewhere.as_posix())

    def test_a_log_export_that_fails_leaves_no_temporary_file(self):
        from applicant.infra.store import sqlite as module

        log = self.root / 'applied_jobs.csv'
        with (
            mock.patch.object(module, 'write_rows', side_effect=OSError('disk full')),
            self.assertRaises(OSError),
        ):
            module._export_log(log, [{'status': 'applied'}])
        self.assertEqual(list(self.root.glob('.applied_jobs.csv.*')), [])
        self.assertFalse(log.exists())


class PppFactorTest(TempDir):
    def table(self, seeded: dict) -> money.Rates:
        with mock.patch.object(money, 'load_factors', return_value=seeded):
            return money.Rates(path=str(self.root / 'cache.json'))

    def test_a_stale_factor_beats_none_when_the_world_bank_will_not_answer(self):
        table = self.table({})
        table._cache['ppp']['SWE'] = {'value': 9.9, 'year': '2020', 'fetched': 0}
        with (
            mock.patch.object(money, 'fetch_factor', return_value=None),
            self.assertLogs('applicant.money', 'WARNING') as logged,
        ):
            self.assertEqual(table.ppp('SEK'), 9.9)
        self.assertIn('using the stale one', logged.output[0])

    def test_no_factor_at_all_falls_back_to_market_rates(self):
        table = self.table({})
        with (
            mock.patch.object(money, 'fetch_factor', return_value=None),
            self.assertLogs('applicant.money', 'WARNING') as logged,
        ):
            self.assertIsNone(table.ppp('SEK'))
        self.assertIn('falling back to market rates', logged.output[0])

    def test_a_throttle_page_is_not_a_series(self):
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text='<html>slow down')
            )
        )
        self.addCleanup(client.close)
        self.assertIsNone(money.fetch_factor('SWE', attempts=2, pause=0, client=client))


class LauncherEdgesTest(unittest.TestCase):
    def test_without_playwright_the_error_says_how_to_install_it(self):
        with (
            mock.patch.dict(sys.modules, {'playwright.sync_api': None}),
            self.assertRaisesRegex(SourceError, 'playwright install'),
        ):
            BrowserSession().start()

    def test_a_session_without_a_profile_needs_a_path_to_save_to(self):
        with self.assertRaisesRegex(SourceError, 'no profile directory'):
            BrowserSession().save_state()

    def test_a_teardown_failure_is_only_logged(self):
        session = BrowserSession()
        broken = mock.Mock()
        broken.close.side_effect = RuntimeError('already closed')
        session._closer = broken
        with self.assertLogs('applicant.infra.browser', 'DEBUG') as logged:
            session.close()
        self.assertIn('already closed', '\n'.join(logged.output))


def posting(job_id: str) -> Job:
    return Job(
        source='linkedin',
        id=job_id,
        title='Dev',
        url=f'https://www.linkedin.com/jobs/view/{job_id}',
    )


class ApplyFailuresTest(TempDir):
    def test_a_client_that_is_blocked_fails_every_row(self):
        client = mock.Mock()
        client.easy_apply.side_effect = Blocked('signed out')
        with self.assertLogs('applicant.services.apply', 'WARNING'):
            entries = easy_apply_with(client, [posting('1'), posting('2')])
        self.assertEqual(
            [(status, note) for _, status, note in entries], [('failed', 'signed out')] * 2
        )

    def test_a_crashing_applier_fails_its_rows_and_the_rest_are_logged(self):
        applier = mock.Mock(source='linkedin')
        applier.apply.side_effect = RuntimeError('boom')
        log = str(self.root / 'applied.csv')
        jobs = [
            posting('1'),
            Job(source='indeed', id='2', title='Ops', url='https://indeed.example/2'),
        ]
        with self.assertLogs('applicant.services.apply', 'ERROR'):
            ApplyToJobs({'linkedin': applier}).run(jobs, log=log, dry_run=False)
        rows = {row['id']: (row['status'], row['note']) for row in ApplicationLog(log).rows()}
        self.assertEqual(rows['1'], ('failed', 'RuntimeError'))
        self.assertEqual(rows['2'][0], 'needs_manual_apply')

    def test_filters_are_applied_before_anything_is_sent(self):
        from applicant.domain.filtering import JobFilter

        applier = mock.Mock(source='linkedin')
        applier.apply.side_effect = lambda jobs, dry_run: [
            ApplicationResult(job, 'would_apply', 'dry run') for job in jobs
        ]
        python, other = (
            posting('1'),
            Job(
                source='linkedin', id='2', title='Chef', url='https://www.linkedin.com/jobs/view/2'
            ),
        )
        python.title = 'Python Dev'
        ApplyToJobs({'linkedin': applier}).run(
            [python, other],
            log=str(self.root / 'a.csv'),
            filters=JobFilter(title='python'),
            dry_run=True,
        )
        self.assertEqual([job.id for job in applier.apply.call_args.args[0]], ['1'])


class AmbitionBoxTransportTest(unittest.TestCase):
    def fetch(self, handler):
        client = AmbitionBoxClient(
            delay=0, client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        return client.fetch('tcs')

    def test_a_server_error_is_reported_by_status(self):
        with self.assertRaisesRegex(SourceError, 'answered HTTP 500'):
            self.fetch(lambda request: httpx.Response(500))

    def test_an_unreachable_site(self):
        def offline(request):
            raise httpx.ConnectError('down', request=request)

        with self.assertRaisesRegex(SourceError, 'could not reach AmbitionBox'):
            self.fetch(offline)

    def test_json_ld_blocks_that_are_broken_or_another_type_are_skipped(self):
        html = (
            '<script type="application/ld+json">{broken</script>'
            '<script type="application/ld+json">{"@type": "Organization"}</script>'
            + ld_json_html(3.7, 900)
        )
        rating = AmbitionBoxClient(delay=0)._from_json_ld(html, 'tcs', 'url')
        self.assertEqual((rating.overall_rating, rating.review_count), (3.7, 900))
        with self.assertRaises(Unparseable):
            AmbitionBoxClient(delay=0)._from_json_ld(html.split('<script')[1], 'tcs', 'url')


if __name__ == '__main__':
    unittest.main()
