"""The CLI, in process.

`applicant.cli` has no import-time side effects, so main() can be called directly
and every handler is reachable without a subprocess.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from applicant.cli import build_parser, main, normalise
from applicant.models import Job
from applicant.storage import ApplicationLog, save_jobs


class NormaliseTest(unittest.TestCase):
    """Bare-flag invocations the README documented before subcommands existed."""

    def test_no_arguments_means_the_linkedin_job_run(self):
        self.assertEqual(normalise([]), ['jobs'])

    def test_a_leading_flag_means_the_linkedin_job_run(self):
        self.assertEqual(normalise(['-c', 'cookies.json']), ['jobs', '-c', 'cookies.json'])

    def test_a_long_leading_flag_too(self):
        self.assertEqual(normalise(['--overwrite']), ['jobs', '--overwrite'])

    def test_help_is_left_alone(self):
        self.assertEqual(normalise(['-h']), ['-h'])
        self.assertEqual(normalise(['--help']), ['--help'])

    def test_a_named_subcommand_is_left_alone(self):
        self.assertEqual(normalise(['search', 'python']), ['search', 'python'])
        self.assertEqual(normalise(['reviews', 'tcs']), ['reviews', 'tcs'])


class ParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_every_subcommand_binds_a_handler(self):
        for argv in (['jobs'], ['search', 'python'], ['apply'], ['reviews', 'tcs'], ['status']):
            with self.subTest(argv=argv):
                self.assertTrue(callable(self.parser.parse_args(argv).handler))

    def test_search_defaults(self):
        args = self.parser.parse_args(['search', 'python developer'])
        self.assertEqual(args.keywords, 'python developer')
        self.assertEqual(args.source, ['all'])
        self.assertEqual(args.limit, 25)
        self.assertEqual(args.output, 'job_listing.json')
        self.assertFalse(args.show)

    def test_search_accepts_several_sources(self):
        args = self.parser.parse_args(['search', 'python', '-s', 'linkedin', 'indeed'])
        self.assertEqual(args.source, ['linkedin', 'indeed'])

    def test_an_unknown_source_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(['search', 'python', '-s', 'monster'])

    def test_the_filter_flags_are_on_both_search_and_apply(self):
        for command in ('search', 'apply'):
            with self.subTest(command=command):
                argv = [command, *(['python'] if command == 'search' else [])]
                args = self.parser.parse_args(
                    [
                        *argv,
                        '--title',
                        'senior',
                        '--min-salary',
                        '1200000',
                        '--currency',
                        'INR',
                        '--posted-within',
                        '7',
                        '--strict',
                    ]
                )
                self.assertEqual(args.title, 'senior')
                self.assertEqual(args.min_salary, 1_200_000)
                self.assertEqual(args.posted_within, 7)
                self.assertTrue(args.strict)

    def test_reviews_defaults_to_ambitionbox(self):
        args = self.parser.parse_args(['reviews', 'tcs'])
        self.assertEqual(args.source, 'ambitionbox')
        self.assertEqual(args.max_reviews, 20)

    def test_reviews_accepts_both(self):
        self.assertEqual(self.parser.parse_args(['reviews', 'tcs', '-s', 'both']).source, 'both')

    def test_display_flag_stores_false(self):
        """-D exists to *show* the browser, so its stored value is headless."""
        self.assertTrue(self.parser.parse_args(['jobs']).Display)
        self.assertFalse(self.parser.parse_args(['jobs', '-D']).Display)


class MainTest(unittest.TestCase):
    def test_the_bare_parser_binds_no_handler(self):
        """Which is exactly why normalise() rewrites an empty argv to `jobs`."""
        self.assertIsNone(getattr(build_parser().parse_args([]), 'handler', None))

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as raised, redirect_stdout(io.StringIO()):
            main(['--help'])
        self.assertEqual(raised.exception.code, 0)

    def test_an_unknown_subcommand_exits_two(self):
        with self.assertRaises(SystemExit) as raised:
            main(['nonsense'])
        self.assertEqual(raised.exception.code, 2)


class StatusCommandTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.listing = str(self.root / 'job_listing.json')
        self.log = str(self.root / 'applied_jobs.csv')

    def run_status(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(['status', '-i', self.listing, '--log', self.log, *extra])
        return code, buffer.getvalue()

    def test_empty_state_reports_zeroes_rather_than_failing(self):
        code, output = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn('0 jobs stored', output)
        self.assertIn('0 applications recorded', output)

    def test_counts_jobs_by_source_and_applications_by_status(self):
        save_jobs(
            [
                Job(source='indeed', id='1', title='A'),
                Job(source='indeed', id='2', title='B'),
                Job(source='linkedin', id='3', title='C'),
            ],
            self.listing,
        )
        ApplicationLog(self.log).record(
            [
                (Job(source='linkedin', id='3', title='C'), 'applied', ''),
                (Job(source='indeed', id='1', title='A'), 'needs_manual_apply', ''),
            ]
        )

        code, output = self.run_status()
        self.assertEqual(code, 0)
        self.assertIn('3 jobs stored', output)
        self.assertIn('indeed: 2', output)
        self.assertIn('linkedin: 1', output)
        self.assertIn('2 applications recorded', output)
        self.assertIn('applied: 1', output)

    def test_json_output_is_machine_readable(self):
        save_jobs([Job(source='indeed', id='1', title='A')], self.listing)
        target = str(self.root / 'status.json')

        code, _ = self.run_status('--json', target)
        self.assertEqual(code, 0)

        with open(target, encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertEqual(payload['jobs']['total'], 1)
        self.assertEqual(payload['jobs']['by_source'], {'indeed': 1})
        self.assertEqual(payload['applications']['total'], 0)


class RatesCommandTest(unittest.TestCase):
    def run_rates(self, *extra: str) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(['rates', *extra])
        return code, buffer.getvalue()

    def test_it_lists_every_mapped_currency(self):
        from applicant.money import CURRENCY_COUNTRY

        code, output = self.run_rates()
        self.assertEqual(code, 0)
        for currency in ('USD', 'GBP', 'INR', 'NZD', 'AUD', 'EUR', 'SEK'):
            with self.subTest(currency=currency):
                self.assertIn(currency, output)
        self.assertIn('of {} mapped currencies'.format(len(set(CURRENCY_COUNTRY.values()))), output)

    def test_a_cached_factor_shows_its_value_and_year(self):
        _, output = self.run_rates()
        self.assertIn('USD (USA): 1.0 per international $ (definition)', output)

    def test_an_uncached_currency_says_so_rather_than_showing_a_number(self):
        _, output = self.run_rates()
        self.assertIn('not cached - fetched on demand', output)


class ApplyCommandTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def test_a_missing_listing_fails_cleanly(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(['apply', '-i', str(self.root / 'nope.json')])

        self.assertEqual(code, 1)
        self.assertIn('no jobs to apply to', buffer.getvalue())

    def test_a_filter_that_matches_nothing_fails_without_opening_a_browser(self):
        listing = str(self.root / 'jobs.json')
        save_jobs([Job(source='indeed', id='1', title='Rust Engineer')], listing)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(['apply', '-i', listing, '--title', 'python'])

        self.assertEqual(code, 1)
        self.assertIn('0 of 1 stored jobs match', buffer.getvalue())

    def test_a_dry_run_records_without_applying(self):
        listing = str(self.root / 'jobs.json')
        log = str(self.root / 'applied.csv')
        save_jobs(
            [
                Job(
                    source='indeed',
                    id='1',
                    title='Python Developer',
                    url='https://indeed.example/1',
                ),
                Job(source='googlejobs', id='2', title='Python Engineer', via='Workday'),
            ],
            listing,
        )

        with redirect_stdout(io.StringIO()):
            code = main(['apply', '-i', listing, '--log', log, '--dry-run', '--title', 'python'])

        self.assertEqual(code, 0)
        rows = ApplicationLog(log).rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['status'] for row in rows}, {'needs_manual_apply'})
        self.assertIn('apply on Workday directly', [row['note'] for row in rows])


if __name__ == '__main__':
    unittest.main()
