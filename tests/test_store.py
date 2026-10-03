"""Settings, applicant.db, and the files it exports.

The promise the SQLite store has to keep: the JSON and CSV it writes are the
files the plain backend would have written, byte for byte, and a directory of
those files from before the database existed is picked up on first use.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from applicant import money
from applicant.cli import main
from applicant.domain.job import Job
from applicant.domain.ports import ApplicationResult
from applicant.errors import ConfigError, StoreError
from applicant.infra.store import repositories
from applicant.infra.store.sqlite import DB_NAME, MIGRATIONS, Store
from applicant.services.apply import ApplyToJobs, easy_apply_with
from applicant.settings import Settings
from applicant.storage import ApplicationLog, load_jobs, save_jobs

FIXED = datetime(2026, 9, 27, 8, 0, 0, tzinfo=timezone.utc)


class TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)


def funded(company: str, total: float):
    from applicant.financials import CompanyFinancials, Money

    return CompanyFinancials(
        source='crunchbase',
        company=company,
        company_id=company,
        total_funding=Money(amount=total, currency='USD', amount_usd=total),
    )


def jobs(*specs) -> list[Job]:
    return [
        Job(source=source, id=job_id, title=title, company=company, location='Pune', url=url)
        for source, job_id, title, company, url in specs
    ]


FIRST = jobs(
    ('indeed', '1', 'Python Dev', 'Acme', 'https://i/1'),
    ('naukri', '2', '=HYPERLINK("x")', 'Evil Co', 'https://n/2'),
    ('googlejobs', None, 'Data Scientist', 'Beta', None),
)
SECOND = jobs(
    ('indeed', '1', 'Python Developer', 'Acme', 'https://i/1'),  # same id, updated
    ('linkedin', '9', 'Data Scientist', 'Beta', 'https://l/9'),  # Beta again, other board
    ('indeed', '3', 'Rust Dev', 'Gamma', 'https://i/3'),
)


# -- Settings ---------------------------------------------------------------


class SettingsTest(TempDir):
    def load(self, **options) -> Settings:
        options.setdefault('environ', {})
        return Settings.load(config=options.pop('config', None), **options)

    def test_the_defaults_are_the_paths_every_run_used_before(self):
        settings = self.load()
        self.assertEqual(settings.path('job_listing.json'), 'job_listing.json')
        self.assertEqual(settings.store, 'sqlite')

    def test_relative_names_live_under_the_data_dir(self):
        settings = self.load(data_dir=str(self.root))
        self.assertEqual(settings.path('applied_jobs.csv'), str(self.root / 'applied_jobs.csv'))
        self.assertEqual(settings.path('/elsewhere/x.csv'), '/elsewhere/x.csv')

    def test_flags_beat_the_environment_which_beats_the_file(self):
        config = self.root / 'applicant.toml'
        config.write_text('data_dir = "from-file"\nstore = "files"\n', encoding='utf-8')
        from_file = self.load(config=str(config))
        # relative in the file: beside the file
        self.assertEqual((from_file.data_dir, from_file.store), (self.root / 'from-file', 'files'))

        env = {'APPLICANT_HOME': 'from-env', 'APPLICANT_STORE': 'sqlite'}
        from_env = self.load(config=str(config), environ=env)
        self.assertEqual((from_env.data_dir, from_env.store), (Path('from-env'), 'sqlite'))

        from_flags = self.load(config=str(config), environ=env, data_dir='from-flag')
        self.assertEqual(from_flags.data_dir, Path('from-flag'))

    def test_a_relative_data_dir_in_the_file_is_beside_the_file(self):
        """The same folder whichever directory the command runs from."""
        conf = self.root / 'conf'
        conf.mkdir()
        (conf / 'applicant.toml').write_text('data_dir = "data"\n', encoding='utf-8')
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        cwd = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, cwd)
        settings = self.load(config=str(conf / 'applicant.toml'))
        self.assertEqual(settings.data_dir, conf / 'data')

    def test_a_relative_data_dir_on_the_command_line_is_the_cwd_s(self):
        config = self.root / 'applicant.toml'
        config.write_text('data_dir = "data"\n', encoding='utf-8')
        self.assertEqual(self.load(config=str(config), data_dir='mine').data_dir, Path('mine'))
        env = {'APPLICANT_HOME': 'theirs'}
        self.assertEqual(self.load(config=str(config), environ=env).data_dir, Path('theirs'))

    def test_absolute_and_home_data_dirs_in_the_file_are_kept(self):
        config = self.root / 'applicant.toml'
        config.write_text('data_dir = "/srv/applicant"\n', encoding='utf-8')
        self.assertEqual(self.load(config=str(config)).data_dir, Path('/srv/applicant'))
        config.write_text('data_dir = "~/applicant"\n', encoding='utf-8')
        self.assertEqual(
            self.load(config=str(config)).data_dir, Path(os.path.expanduser('~/applicant'))
        )

    def test_keys_are_refused_in_the_config_file(self):
        config = self.root / 'applicant.toml'
        config.write_text('tracxn_key = "t0ken-in-a-file"\n', encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'belong in the environment'):
            self.load(config=str(config))

    def test_a_bad_value_names_where_it_is(self):
        with self.assertRaisesRegex(ConfigError, 'store'):
            self.load(store='postgres')

    def test_a_named_config_file_must_exist(self):
        with self.assertRaisesRegex(ConfigError, 'does not exist'):
            self.load(config=str(self.root / 'missing.toml'))

    def test_keys_are_secret_values(self):
        settings = self.load(environ={'CRUNCHBASE_API_KEY': 'cb-key-0123456789'})
        self.assertNotIn('cb-key-0123456789', repr(settings))
        self.assertEqual(settings.secret('crunchbase_key'), 'cb-key-0123456789')


# -- the store keeps the files' promises -------------------------------------


class ExportTest(TempDir):
    """Same writes through both backends; the files must come out identical."""

    def run_both(self, write) -> tuple[Path, Path]:
        plain, stored = self.root / 'plain', self.root / 'stored'
        plain.mkdir()
        stored.mkdir()
        with mock.patch('applicant.storage.datetime') as clock:
            clock.now.return_value = FIXED
            write(plain, 'files')
            write(stored, 'sqlite')
        return plain, stored

    def test_the_listing_json_is_byte_identical(self):
        def write(folder, backend):
            listing = repositories.listing(folder / 'job_listing.json', backend)
            listing.save(FIRST)
            listing.save(SECOND)

        plain, stored = self.run_both(write)
        self.assertEqual(
            (plain / 'job_listing.json').read_bytes(), (stored / 'job_listing.json').read_bytes()
        )
        self.assertTrue((stored / DB_NAME).exists())
        self.assertFalse((plain / DB_NAME).exists(), 'the files backend makes no database')

    def test_the_application_csv_is_byte_identical(self):
        """Appended by one, rewritten whole by the other - the same bytes."""

        def write(folder, backend):
            log = repositories.applications(folder / 'applied_jobs.csv', backend)
            log.record([(job, 'applied', 'first') for job in FIRST])
            log.record([(job, 'needs_manual_apply', 'second') for job in SECOND])

        plain, stored = self.run_both(write)
        expected = (plain / 'applied_jobs.csv').read_bytes()
        self.assertEqual((stored / 'applied_jobs.csv').read_bytes(), expected)
        self.assertTrue(expected.startswith('\ufeff"applied_at"'.encode()), 'BOM, quoted header')
        self.assertIn(b"'=HYPERLINK", expected, 'formula starts are still neutralised')

    def test_both_backends_dedupe_alike(self):
        """Same board, same id: skipped. Same job on another board: skipped."""
        counts = {}
        for backend in ('files', 'sqlite'):
            folder = self.root / backend
            folder.mkdir()
            log = repositories.applications(folder / 'applied_jobs.csv', backend)
            log.record([(job, 'applied', '') for job in FIRST])
            written = log.record([(job, 'applied', '') for job in SECOND])
            counts[backend] = (written, log.counts())
        self.assertEqual(counts['files'], counts['sqlite'])
        self.assertEqual(counts['sqlite'][0], 1, 'only Rust Dev is new')


class MigrationTest(TempDir):
    """A working directory from before the database: picked up on first use."""

    def old_directory(self) -> tuple[str, str]:
        listing, log = str(self.root / 'job_listing.json'), str(self.root / 'applied_jobs.csv')
        save_jobs(FIRST, listing)
        ApplicationLog(log).record([(FIRST[0], 'applied', 'by hand, last year')])
        return listing, log

    def test_status_imports_the_old_files(self):
        listing, log = self.old_directory()
        before = Path(log).read_bytes()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(['status', '-i', listing, '--log', log, '--no-log-file'])

        self.assertEqual(code, 0)
        self.assertIn('3 jobs stored', out.getvalue())
        self.assertIn('applied: 1', out.getvalue())
        with Store.beside(listing) as store:
            self.assertEqual(len(store.job_items(listing)), 3)
            self.assertEqual(len(store.application_rows(log)), 1)
        self.assertEqual(Path(log).read_bytes(), before, 'reading exports nothing')

    def test_an_apply_after_import_skips_what_the_old_log_holds(self):
        listing, log = self.old_directory()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(['apply', '-i', listing, '--log', log, '--dry-run', '--no-log-file'])
        rows = ApplicationLog(log).rows()
        self.assertEqual(len(rows), 3, "the two new ones, and last year's kept")
        self.assertEqual(rows[0]['note'], 'by hand, last year')

    def test_the_files_store_makes_no_database(self):
        listing, log = self.old_directory()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(['status', '-i', listing, '--log', log, '--no-log-file', '--store', 'files'])
        self.assertFalse((self.root / DB_NAME).exists())

    def test_looking_at_an_empty_directory_leaves_nothing_behind(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(['status', '--data-dir', str(self.root), '--no-log-file'])
        self.assertEqual(list(self.root.iterdir()), [])


class SyncTest(TempDir):
    """The file you see is the data the next run uses."""

    def setUp(self):
        super().setUp()
        self.listing = str(self.root / 'job_listing.json')
        self.store = Store.beside(self.listing)
        self.addCleanup(self.store.close)
        self.store.save_jobs(FIRST, self.listing)

    def titles(self) -> list[str]:
        return [item['title'] for item in self.store.job_items(self.listing)]

    def test_a_hand_edit_wins(self):
        document = json.loads(Path(self.listing).read_text(encoding='utf-8'))
        document['list'] = document['list'][:1]
        Path(self.listing).write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(self.titles(), ['Python Dev'])

    def test_deleting_the_file_empties_the_listing(self):
        os.remove(self.listing)
        self.assertEqual(self.titles(), [])

    def test_an_unreadable_file_is_not_destroyed_by_a_read(self):
        Path(self.listing).write_text('{"list": [', encoding='utf-8')
        self.assertEqual(self.titles(), [])
        self.assertEqual(Path(self.listing).read_text(encoding='utf-8'), '{"list": [')

    def test_an_unreadable_file_is_moved_aside_by_a_write(self):
        """Moved aside - and the jobs the database held are not lost with it."""
        Path(self.listing).write_text('{"list": [', encoding='utf-8')
        with self.assertLogs('applicant', 'WARNING') as logged:
            self.store.save_jobs(SECOND, self.listing)
        self.assertEqual(len([p for p in self.root.iterdir() if '.corrupt-' in p.name]), 1)
        self.assertIn('rewritten from the 3 job(s)', '\n'.join(logged.output))
        self.assertEqual(len(self.titles()), 5, 'the three stored, and two new')
        self.assertEqual(len(load_jobs(self.listing)), 5, 'the file is whole again')

    def test_an_unreadable_file_with_nothing_stored_starts_afresh(self):
        other = str(self.root / 'other.json')
        Path(other).write_text('{"list": [', encoding='utf-8')
        with self.assertLogs('applicant.files', 'WARNING'):
            self.store.save_jobs(SECOND, other)
        self.assertEqual(len(self.store.job_items(other)), 3)

    def test_an_unreadable_financials_file_keeps_the_history(self):
        from applicant.financials.tracker import FinancialsTracker

        path = str(self.root / 'company_financials.json')
        self.store.close()
        tracker = FinancialsTracker(path, backend='sqlite')
        tracker.record(funded('zomato', 1e9))
        tracker.record(funded('zomato', 2e9))
        Path(path).write_text('{"companies": {', encoding='utf-8')
        with self.assertLogs('applicant', 'WARNING'):
            again = FinancialsTracker(path, backend='sqlite')
        self.assertEqual(len(again.history('crunchbase:zomato')), 2)
        self.assertEqual(len(json.loads(Path(path).read_text())['companies']), 1)

    def test_moving_the_directory_keeps_every_collection(self):
        self.store.close()
        moved = self.root.parent / (self.root.name + '-moved')
        os.rename(self.root, moved)
        self.addCleanup(lambda: moved.exists() and os.rename(moved, self.root))
        with (
            Store.beside(moved / 'job_listing.json') as store,
            self.assertNoLogs('applicant', 'INFO'),
        ):
            self.assertEqual(len(store.job_items(moved / 'job_listing.json')), 3)

    def test_the_database_is_owner_only_and_versioned(self):
        path = self.root / DB_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        version = self.store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(version, len(MIGRATIONS))


class RatingHistoryTest(TempDir):
    """company_reviews.json is overwritten each run; the database keeps them all."""

    def test_ratings_accumulate(self):
        with Store(self.root / DB_NAME) as store:
            store.add_ratings('TCS', [('ambitionbox', {'overall_rating': 3.7})])
            store.add_ratings('tcs', [('ambitionbox', {'overall_rating': 3.8})])
            history = store.rating_history('tcs', 'ambitionbox')
        self.assertEqual([item['overall_rating'] for item in history], [3.7, 3.8])
        self.assertTrue(all('fetched_at' in item for item in history))


class FinancialsHistoryTest(TempDir):
    """0.2.0: the financials change log lives in applicant.db too, exported as before."""

    def financials(self, total: float, stage: str = 'Series J'):
        from applicant.financials import CompanyFinancials, Money

        return CompanyFinancials(
            source='crunchbase',
            company='Zomato',
            company_id='zomato',
            total_funding=Money(amount=total, currency='USD', amount_usd=total),
            stage=stage,
            # pinned: it defaults to the current second, and two backends' runs
            # straddling a second boundary would differ for that reason alone
            fetched_at=FIXED.isoformat(),
        )

    def track(self, folder: Path, backend: str) -> Path:
        from applicant.financials import FinancialsTracker

        folder.mkdir(exist_ok=True)
        path = folder / 'company_financials.json'
        with mock.patch('applicant.financials.tracker.datetime') as clock:
            clock.now.return_value = FIXED
            FinancialsTracker(str(path), backend=backend).record(self.financials(2.1e9))
            FinancialsTracker(str(path), backend=backend).record(self.financials(2.4e9, 'Series K'))
            FinancialsTracker(str(path), backend=backend).record(self.financials(2.4e9, 'Series K'))
        return path

    def test_the_history_json_is_byte_identical(self):
        plain = self.track(self.root / 'plain', 'files')
        stored = self.track(self.root / 'stored', 'sqlite')
        self.assertEqual(plain.read_bytes(), stored.read_bytes())
        self.assertTrue((self.root / 'stored' / DB_NAME).exists())

    def test_the_database_holds_the_change_log(self):
        path = self.track(self.root / 'stored', 'sqlite')
        with Store.beside(path) as store:
            history = store.financial_history(path, 'crunchbase:zomato')
            rebuilt = store.financials_document(path)
        self.assertEqual(len(history), 2, 'first sight, then the change; not the repeat')
        self.assertIn('total funding', history[1]['changes'][0])
        self.assertEqual(
            json.dumps(rebuilt, ensure_ascii=False),
            json.dumps(json.loads(path.read_text(encoding='utf-8')), ensure_ascii=False),
            'rebuilt with every key in the order the file has it',
        )

    def test_an_old_history_file_is_imported_on_first_use(self):
        from applicant.financials import FinancialsTracker

        path = self.track(self.root / 'old', 'files')
        tracker = FinancialsTracker(str(path), backend='sqlite')
        self.assertEqual(len(tracker.history('crunchbase:zomato')), 2)
        self.assertEqual(tracker.record(self.financials(2.4e9, 'Series K')), [])

    def test_a_hand_edit_wins(self):
        from applicant.financials import FinancialsTracker

        path = self.track(self.root / 'stored', 'sqlite')
        document = json.loads(path.read_text(encoding='utf-8'))
        del document['companies']['crunchbase:zomato']
        path.write_text(json.dumps(document), encoding='utf-8')
        self.assertEqual(FinancialsTracker(str(path), backend='sqlite').data['companies'], {})


# -- the defects that fell out: D6 and D10 -----------------------------------


class LocalFactorsTest(TempDir):
    """D6: refreshed PPP factors used to be written into the installed package."""

    def test_a_refresh_writes_beside_the_data_and_the_package_is_untouched(self):
        shipped = money.FACTORS_PATH.read_bytes()
        local = self.root / 'ppp_factors.json'
        self.addCleanup(money.configure, factors='ppp_factors.json')
        money.configure(factors=local)

        with mock.patch.object(money, 'fetch_factor', return_value={'value': 0.7, 'year': '2025'}):
            updated, _, skipped = money.refresh_factors(currencies=['GBP', 'INR'], pause=0)

        self.assertEqual(updated, ['GBR'])
        self.assertIn('IND', skipped, 'the shipped table counts as known')
        self.assertEqual(money.FACTORS_PATH.read_bytes(), shipped)
        self.assertEqual(json.loads(local.read_text())['factors']['GBR']['value'], 0.7)
        self.assertEqual(money.load_factors()['GBR']['value'], 0.7, 'layered over the shipped')
        self.assertIn('USA', money.load_factors())


class EasyApplyHandoverTest(TempDir):
    """D10: jobs used to be staged in a file for the LinkedIn client to read back."""

    def test_jobs_are_handed_over_directly(self):
        client = mock.Mock()
        client.easy_apply.return_value = ['https://l/9']
        cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, cwd)

        entries = easy_apply_with(client, [SECOND[1]])

        (handed,) = client.easy_apply.call_args.args
        self.assertEqual(handed, [SECOND[1]])
        self.assertEqual(entries[0][1], 'applied')
        self.assertEqual(list(self.root.iterdir()), [], 'no staging file')


class ConfigExitTest(TempDir):
    def test_bad_settings_are_a_usage_error(self):
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = main(['status', '--config', str(self.root / 'nope.toml'), '--no-log-file'])
        self.assertEqual(code, 2)
        self.assertIn('does not exist', err.getvalue())


# -- what the log counts as done --------------------------------------------

BACKENDS: tuple[repositories.Backend, ...] = ('files', 'sqlite')
JOB = jobs(('linkedin', '7', 'ML Engineer', 'Acme', 'https://l/7'))[0]


class OutcomeTest(TempDir):
    """Only an outcome stops another row: a dry run or a failure is an attempt."""

    def statuses(self, backend: repositories.Backend, *steps: str) -> list[str]:
        log = repositories.applications(str(self.root / backend / 'applied.csv'), backend)
        for status in steps:
            log.record([(JOB, status, status)])
        return [row['status'] for row in log.rows()]

    def test_the_real_application_is_logged_after_a_dry_run(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                self.assertEqual(
                    self.statuses(backend, 'would_apply', 'applied'), ['would_apply', 'applied']
                )

    def test_a_success_is_logged_after_a_failure(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                self.assertEqual(self.statuses(backend, 'failed', 'applied'), ['failed', 'applied'])

    def test_an_outcome_still_blocks_a_second_row(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                self.assertEqual(self.statuses(backend, 'applied', 'applied'), ['applied'])

    def test_the_two_backends_export_the_same_bytes(self):
        for backend in BACKENDS:
            self.statuses(backend, 'would_apply', 'failed', 'applied')
        files, sqlite = (self.root / b / 'applied.csv' for b in BACKENDS)
        # applied_at differs by the second at most; the rest must match
        self.assertEqual(
            [line.split(',')[1:] for line in files.read_text(encoding='utf-8-sig').splitlines()],
            [line.split(',')[1:] for line in sqlite.read_text(encoding='utf-8-sig').splitlines()],
        )


class SettledTest(TempDir):
    """A job the log shows as settled - applied to, or needing a person - is not
    submitted, or offered, again."""

    def run_apply(self, backend: repositories.Backend, log: str) -> tuple[list, list]:
        submitted: list = []
        offered: list = []

        class Applier:
            source = 'linkedin'

            def apply(self, jobs: list[Job], *, dry_run: bool) -> list[ApplicationResult]:
                submitted.extend(jobs)
                return [ApplicationResult(job, 'applied', 'test') for job in jobs]

        def confirm(targets):
            offered.extend(targets)
            return True

        other = jobs(('linkedin', '8', 'Data Scientist', 'Beta', 'https://l/8'))[0]
        ApplyToJobs({'linkedin': Applier()}, backend=backend, confirm=confirm).run(
            [JOB, other], log=log, dry_run=False
        )
        return submitted, offered

    def test_an_applied_job_is_skipped(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                log = str(self.root / backend / 'applied.csv')
                repositories.applications(log, backend).record([(JOB, 'applied', 'earlier')])
                submitted, offered = self.run_apply(backend, log)
                self.assertEqual([job.id for job in submitted], ['8'])
                self.assertEqual([job.id for job in offered], ['8'])

    def test_the_same_job_applied_to_through_another_board_is_skipped(self):
        log = str(self.root / 'applied.csv')
        elsewhere = JOB.model_copy(update={'source': 'indeed', 'id': 'i-7'})
        ApplicationLog(log).record([(elsewhere, 'applied', 'by hand')])
        submitted, _ = self.run_apply('files', log)
        self.assertEqual([job.id for job in submitted], ['8'])

    def test_a_look_alike_posting_on_the_same_board_is_not_skipped(self):
        """Two ids on one board are two postings, as in the log itself."""
        log = str(self.root / 'applied.csv')
        twin = JOB.model_copy(update={'id': '70'})
        ApplicationLog(log).record([(twin, 'applied', 'earlier')])
        submitted, _ = self.run_apply('files', log)
        self.assertEqual(sorted(job.id for job in submitted), ['7', '8'])

    def test_a_job_that_needs_a_person_is_not_offered_again(self):
        """A multi step form last time is a multi step form this time."""
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                log = str(self.root / backend / 'applied.csv')
                repositories.applications(log, backend).record(
                    [(JOB, 'needs_manual_apply', 'not easy apply, or a multi step form')]
                )
                submitted, offered = self.run_apply(backend, log)
                self.assertEqual([job.id for job in submitted], ['8'])
                self.assertEqual([job.id for job in offered], ['8'])

    def test_a_listing_on_another_board_does_not_settle_the_easy_apply_copy(self):
        """'Apply on indeed directly' is a worklist entry, not an application."""
        log = str(self.root / 'applied.csv')
        elsewhere = JOB.model_copy(update={'source': 'indeed', 'id': 'i-7'})
        ApplicationLog(log).record([(elsewhere, 'needs_manual_apply', 'apply on indeed directly')])
        submitted, _ = self.run_apply('files', log)
        self.assertEqual(sorted(job.id for job in submitted), ['7', '8'])

    def test_a_failed_job_is_offered_again(self):
        log = str(self.root / 'applied.csv')
        ApplicationLog(log).record([(JOB, 'failed', 'browser crashed')])
        submitted, _ = self.run_apply('files', log)
        self.assertEqual(sorted(job.id for job in submitted), ['7', '8'])

    def test_a_rehearsed_job_is_still_submitted(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                log = str(self.root / backend / 'applied.csv')
                repositories.applications(log, backend).record([(JOB, 'would_apply', 'dry run')])
                submitted, _ = self.run_apply(backend, log)
                self.assertEqual(sorted(job.id for job in submitted), ['7', '8'])


class WrittenFilesTest(TempDir):
    """New directories are made; a new application log is owner-only."""

    def mode(self, path: Path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_a_new_data_directory_is_made_on_first_write(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                where = self.root / backend / 'not' / 'yet'
                repositories.listing(str(where / 'jobs.json'), backend).save([JOB])
                repositories.applications(str(where / 'applied.csv'), backend).record(
                    [(JOB, 'applied', 'x')]
                )
                self.assertTrue((where / 'jobs.json').exists())
                self.assertTrue((where / 'applied.csv').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_a_new_log_is_owner_only(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                log = self.root / backend / 'applied.csv'
                repositories.applications(str(log), backend).record([(JOB, 'applied', 'x')])
                self.assertEqual(self.mode(log), 0o600)

    @unittest.skipIf(os.name == 'nt', 'POSIX permissions')
    def test_an_existing_log_keeps_its_mode(self):
        for backend in BACKENDS:
            with self.subTest(backend=backend):
                log = self.root / backend / 'applied.csv'
                log.parent.mkdir(parents=True)
                ApplicationLog(str(log)).record([(JOB, 'would_apply', 'x')])
                os.chmod(log, 0o640)
                repositories.applications(str(log), backend).record([(JOB, 'applied', 'x')])
                self.assertEqual(self.mode(log), 0o640)

    def test_the_export_leaves_no_temporary_file(self):
        log = self.root / 'applied.csv'
        record = repositories.applications(str(log), 'sqlite')
        record.record([(JOB, 'would_apply', 'x')])
        record.record([(JOB, 'applied', 'x')])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [DB_NAME, 'applied.csv'])


class AtomicMigrationTest(TempDir):
    def test_a_failing_migration_changes_nothing(self):
        broken = ((*MIGRATIONS[0], 'CREATE TABLE half ('),)
        path = self.root / DB_NAME
        with (
            mock.patch('applicant.infra.store.sqlite.MIGRATIONS', broken),
            self.assertRaises(StoreError),
        ):
            Store(path)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        self.assertEqual(
            db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), []
        )
        db.close()
        # and the real schema then applies cleanly
        with Store(path) as store:
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))


if __name__ == '__main__':
    unittest.main()
