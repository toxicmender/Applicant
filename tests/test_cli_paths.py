"""The CLI's remaining exits, and the branches that are behaviour rather than
defence: what each says, and the code it ends with.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from applicant import log
from applicant.boards.linkedin_apply import LinkedIn
from applicant.cli import main
from applicant.domain.rates import RateSnapshot
from applicant.financials.models import FundingRound, Money
from applicant.financials.tracker import describe_round
from applicant.services import rates as rates_service
from applicant.services.financials import Tracked
from applicant.services.reviews import Reviews, fetch_reviews


class Cli(unittest.TestCase):
    def setUp(self):
        # main() configures logging for the process; leave it as found
        self.addCleanup(log.silence)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def run_main(self, *argv: str, log: bool = False) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        flags = [] if log else ['--no-log-file']
        with redirect_stdout(out), redirect_stderr(err):
            code = main([*argv, '--data-dir', str(self.root), *flags])
        return code, out.getvalue(), err.getvalue()


class ExitsTest(Cli):
    def test_a_log_file_that_cannot_be_opened_is_a_usage_error(self):
        # a directory where the log file should be
        code, _, err = self.run_main('status', '--log-file', str(self.root), log=True)
        self.assertEqual(code, 2)
        self.assertIn('could not open', err)

    @unittest.skipIf(not hasattr(os, 'geteuid') or os.geteuid() == 0, 'root can write anywhere')
    def test_a_log_file_in_a_folder_that_cannot_be_written_is_a_usage_error(self):
        locked = self.root / 'locked'
        locked.mkdir(mode=0o500)
        self.addCleanup(locked.chmod, 0o700)
        code, _, err = self.run_main('status', '--log-file', str(locked / 'run.log'), log=True)
        self.assertEqual(code, 2)
        self.assertIn('not writable', err)

    def test_an_expired_linkedin_session_says_how_to_sign_in_again(self):
        cookies = self.root / 'cookies.json'
        cookies.write_text('{"cookies": []}', encoding='utf-8')
        with mock.patch.object(LinkedIn, 'restore_session', return_value=False):
            code, _, err = self.run_main('jobs', '--cookies', str(cookies))
        self.assertEqual(code, 1)
        self.assertIn('rerun with --overwrite', err)

    def test_financials_that_found_nothing_exit_1(self):
        nothing = Tracked(companies=1, found=0, output=str(self.root / 'f.json'))
        with mock.patch('applicant.services.financials.track_financials', return_value=nothing):
            code, out, _ = self.run_main('financials', 'zomato')
        self.assertEqual(code, 1)
        self.assertNotIn('tracked in', out)

    def test_no_saved_searches_to_list(self):
        code, out, _ = self.run_main('search', '--list-saved')
        self.assertEqual(code, 0)
        self.assertIn('no saved searches', out)

    def test_a_count_that_is_not_a_number_is_a_usage_error(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as stopped:
            main(['search', 'python', '--limit', 'ten', '--no-log-file'])
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("'ten' is not a whole number", err.getvalue())

    def test_a_refresh_with_nothing_to_fetch_still_shows_the_table(self):
        known = rates_service.Refreshed(updated=[], failed=[], skipped=['SWE'], path='x')
        with mock.patch('applicant.services.rates.refresh', return_value=known):
            code, out, _ = self.run_main('rates', '--refresh', '-c', 'SEK')
        self.assertEqual(code, 0)
        self.assertIn('mapped currencies have a local PPP factor', out)


class BehaviourTest(unittest.TestCase):
    def test_rates_based_on_a_currency_with_no_rate_are_none(self):
        snapshot = RateSnapshot(market={'USD': 1.0, 'INR': 83.0})
        self.assertEqual(snapshot.fx('EUR'), {})
        self.assertEqual(snapshot.fx('inr')['USD'], 1 / 83.0)

    def test_a_rate_table_of_the_caller_s_own_is_trusted_as_given(self):
        from applicant.domain.job import Job
        from applicant.filters import JobFilter, prepared

        own = mock.Mock(spec=['market', 'ppp'])  # no snapshot()
        filters = JobFilter(min_salary=10, currency='USD', rates=own)
        self.assertIs(prepared(filters, [Job(source='x', title='t', salary='₹10 LPA')]), filters)

    def test_a_round_described_with_everything_it_says(self):
        described = describe_round(
            FundingRound(
                date='2021-02-17',
                round='Series J',
                amount=Money(amount=2.5e8, currency='USD'),
                post_money_valuation=Money(amount=5.4e9, currency='USD'),
                lead_investors=['Tiger Global', 'Kora'],
            )
        )
        self.assertEqual(
            described, 'Series J on 2021-02-17 ($250M; valued at $5.4B; led by Tiger Global, Kora)'
        )

    def test_factors_that_did_not_come_back_are_named(self):
        with (
            mock.patch.object(
                rates_service, 'refresh_factors', return_value=([], ['NOR', 'SWE'], [])
            ),
            self.assertLogs('applicant.services.rates', 'WARNING') as logged,
        ):
            done = rates_service.refresh(['NOK', 'SEK'])
        self.assertEqual(done.failed, ['NOR', 'SWE'])
        self.assertIn('no PPP factor returned for NOR, SWE', '\n'.join(logged.output))

    def test_sources_that_answer_with_nothing_write_nothing(self):
        silent = mock.Mock()
        silent.fetch.return_value = None
        with tempfile.TemporaryDirectory() as folder, self.assertLogs('applicant', 'ERROR'):
            found = fetch_reviews('tcs', {'ambitionbox': silent}, str(Path(folder) / 'r.json'))
        self.assertEqual(found, Reviews([], None))


if __name__ == '__main__':
    unittest.main()
