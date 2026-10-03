"""Exceptional conditions, checked against OWASP Top 10:2025 A10 and ASVS 5.0 V16.

Each class is one finding from the audit: what used to happen, and the test that
keeps it from happening again.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import httpx

from applicant import log
from applicant.boards import Capability
from applicant.boards.linkedin_apply import LinkedIn
from applicant.cli import main
from applicant.domain.job import Job
from applicant.errors import SourceError, Unparseable
from applicant.files import read_document, write_document
from applicant.financials import (
    CompanyFinancials,
    CrunchbaseClient,
    FinancialsTracker,
    Money,
    TracxnClient,
)
from applicant.money import refresh_factors
from applicant.reviews import AmbitionBoxClient, GlassdoorClient
from applicant.search import Jobs
from applicant.storage import ApplicationLog, load_jobs, save_jobs


class TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        # keep the tests' own stderr quiet; main() reconfigures logging per run
        self.addCleanup(log.silence)

    def leftovers(self, name: str) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if path.name != name)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def offline(request):
    raise httpx.ConnectError('network is unreachable', request=request)


# -- CWE-248 / ASVS 16.5.4: an unhandled exception took the process down ------


class LastResortHandlerTest(TempDir):
    def run_main(self, error: BaseException) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch('applicant.cli.run_status', side_effect=error),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            # the log is not what these check; keep it out of the working directory
            code = main(['status', '--no-log-file'])
        return code, out.getvalue(), err.getvalue()

    def test_an_unexpected_error_is_one_line_not_a_traceback(self):
        """ASVS 16.5.1: a generic message out, the detail in the log."""
        code, _, err = self.run_main(RuntimeError('disk on fire'))
        self.assertEqual(code, 1)
        self.assertIn('status failed unexpectedly: RuntimeError: disk on fire', err)
        self.assertNotIn('Traceback', err)

    def test_a_store_error_is_reported_as_itself_not_as_a_crash(self):
        from applicant.errors import StoreError

        code, _, err = self.run_main(StoreError('could not open applicant.db: read-only'))
        self.assertEqual(code, 1)
        self.assertIn('status: could not open applicant.db: read-only', err)
        self.assertNotIn('unexpectedly', err)
        self.assertEqual(len(err.strip().splitlines()), 1)

    def test_a_network_that_never_answered_exits_7(self):
        from applicant.errors import Unreachable

        code, _, err = self.run_main(Unreachable('could not reach Indeed: timed out'))
        self.assertEqual(code, 7)
        self.assertNotIn('unexpectedly', err)

    def test_the_traceback_still_reaches_the_debug_log(self):
        path = str(self.root / 'run.log')
        with (
            mock.patch('applicant.cli.run_status', side_effect=RuntimeError('boom')),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            main(['status', '--log-file', path])
        self.assertIn('Traceback', Path(path).read_text(encoding='utf-8'))

    def test_ctrl_c_exits_130_quietly(self):
        code, _, err = self.run_main(KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertNotIn('Traceback', err)


# -- ASVS 16.5.2: one failing board discarded every board's results -----------


class FakeBoard:
    # what the Board protocol asks of every board: filters nothing, publishes nothing
    capability = Capability()

    def __init__(self, result):
        self.result = result
        self.closed = False

    def search(self, *args, **kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


class BoardIsolationTest(unittest.TestCase):
    def search(self, boards: dict) -> list[Job]:
        facade = Jobs(sources=list(boards))
        with (
            mock.patch.object(Jobs, '_client', lambda self, name: boards[name]),
            redirect_stdout(io.StringIO()),
        ):
            return facade.search('python')

    def test_an_unexpected_error_on_one_board_keeps_the_others(self):
        good = FakeBoard([Job(source='indeed', id='1', title='Python Dev')])
        broken = FakeBoard(TimeoutError('navigation timed out'))
        found = self.search({'naukri': broken, 'indeed': good})
        self.assertEqual([job.id for job in found], ['1'])
        self.assertTrue(broken.closed, 'a failed board must still be closed')

    def test_a_board_that_fails_to_close_does_not_mask_the_result(self):
        board = FakeBoard([Job(source='indeed', id='1', title='Python Dev')])
        board.close = mock.Mock(side_effect=RuntimeError('already closed'))
        self.assertEqual(len(self.search({'indeed': board})), 1)


# -- A10 "roll back or complete": applications were lost part way -------------


class EasyApplyTest(TempDir):
    def test_applications_already_sent_survive_a_later_failure(self):
        listing = str(self.root / 'jobs.json')
        stored = [
            {'source': 'linkedin', 'url': f'https://linkedin.example/{n}', 'title': str(n)}
            for n in range(3)
        ]
        Path(listing).write_text(json.dumps({'list': stored}), encoding='utf-8')

        client = LinkedIn(client=mock_client(offline))
        self.addCleanup(client.close)
        outcomes = [True, RuntimeError('page crashed'), True]
        with (
            mock.patch.object(LinkedIn, '_start', return_value=mock.Mock()),
            mock.patch.object(LinkedIn, '_apply_one', side_effect=outcomes),
            redirect_stdout(io.StringIO()),
        ):
            applied = client.easy_apply(listing)

        self.assertEqual(applied, ['https://linkedin.example/0', 'https://linkedin.example/2'])

    def test_a_browser_that_will_not_start_still_logs_every_other_row(self):
        log = str(self.root / 'applied.csv')
        linkedin = mock.Mock()
        linkedin.easy_apply.side_effect = RuntimeError('no browser')
        jobs = [
            Job(source='linkedin', id='1', title='Dev', url='https://linkedin.example/1'),
            Job(source='indeed', id='2', title='Dev', url='https://indeed.example/2'),
        ]
        with (
            redirect_stdout(io.StringIO()),
        ):
            Jobs(linkedin=linkedin).apply(jobs, log=log, dry_run=False)

        statuses = {row['id']: row['status'] for row in ApplicationLog(log).rows()}
        self.assertEqual(statuses, {'1': 'failed', '2': 'needs_manual_apply'})


# -- CWE-755: transport and HTTP errors escaped as non-domain exceptions -------


class LinkedInGuestSearchErrorTest(unittest.TestCase):
    def client(self, handler) -> LinkedIn:
        instance = LinkedIn(delay=0, client=mock_client(handler))
        self.addCleanup(instance.close)
        return instance

    def test_a_server_error_is_a_jobs_error(self):
        with self.assertRaisesRegex(SourceError, 'HTTP 500'):
            self.client(lambda request: httpx.Response(500)).search('python')

    def test_an_unreachable_host_is_a_jobs_error(self):
        with self.assertRaisesRegex(SourceError, 'could not reach LinkedIn'):
            self.client(offline).search('python')


class ReviewsErrorTest(unittest.TestCase):
    def ambitionbox(self, handler) -> AmbitionBoxClient:
        return AmbitionBoxClient(delay=0, retries=1, client=mock_client(handler))

    def test_ambitionbox_offline_is_a_reviews_error(self):
        with self.assertRaisesRegex(SourceError, 'could not reach AmbitionBox'):
            self.ambitionbox(offline).fetch('tcs')

    def test_ambitionbox_server_error_is_a_reviews_error(self):
        with self.assertRaisesRegex(SourceError, 'HTTP 500'):
            self.ambitionbox(lambda request: httpx.Response(500)).fetch('tcs')

    def test_a_glassdoor_browser_failure_is_a_reviews_error(self):
        with (
            mock.patch.object(GlassdoorClient, '_fetch', side_effect=RuntimeError('no chrome')),
            self.assertRaisesRegex(SourceError, 'browser session failed: RuntimeError'),
        ):
            GlassdoorClient().fetch('Google-E9079')

    def test_one_failing_source_no_longer_ends_the_reviews_run(self):
        err = io.StringIO()
        with (
            mock.patch.object(GlassdoorClient, '_fetch', side_effect=RuntimeError('no chrome')),
            mock.patch.object(
                AmbitionBoxClient, '_fetch', side_effect=httpx.ConnectError('offline')
            ),
            redirect_stdout(io.StringIO()),
            redirect_stderr(err),
        ):
            code = main(
                ['reviews', 'Google-E9079', '-s', 'both', '-o', os.devnull, '--no-log-file']
            )
        self.assertEqual(code, 1)  # nothing fetched, but reported rather than raised
        # a failed source is commentary, so it is reported on stderr
        self.assertIn('ambitionbox: could not reach AmbitionBox', err.getvalue())
        self.assertIn('glassdoor: the Glassdoor browser session failed', err.getvalue())


class FinancialsErrorTest(unittest.TestCase):
    def test_a_crunchbase_server_error_is_a_financials_error(self):
        client = CrunchbaseClient(
            api_key='k', delay=0, client=mock_client(lambda request: httpx.Response(500))
        )
        with self.assertRaisesRegex(SourceError, 'HTTP 500'):
            client.fetch('zomato')

    def test_a_crunchbase_body_that_is_not_json_is_a_parse_error(self):
        client = CrunchbaseClient(
            api_key='k', delay=0, client=mock_client(lambda request: httpx.Response(200, text='<'))
        )
        with self.assertRaises(Unparseable):
            client.fetch('zomato')

    def test_a_tracxn_server_error_is_a_financials_error(self):
        client = TracxnClient(
            api_key='t', delay=0, client=mock_client(lambda request: httpx.Response(500))
        )
        with self.assertRaisesRegex(SourceError, 'HTTP 500'):
            client.fetch('5c1c697b8f088f5b6f55226c')

    def test_constructing_a_client_registers_its_key_for_masking(self):
        self.addCleanup(log._secrets.clear)
        CrunchbaseClient(api_key='crunchbase-key-xyz')
        self.assertIn('crunchbase-key-xyz', log._secrets)


# -- CWE-390 / CWE-636: an unreadable store was read as empty, then overwritten


class ListingStorageTest(TempDir):
    def test_an_unreadable_listing_is_moved_aside_not_overwritten(self):
        listing = self.root / 'jobs.json'
        listing.write_text('{"list": [ truncated', encoding='utf-8')

        with self.assertLogs('applicant.files', 'WARNING'):
            save_jobs([Job(source='indeed', id='1', title='Dev')], str(listing))

        kept = [name for name in self.leftovers('jobs.json') if '.corrupt-' in name]
        self.assertEqual(len(kept), 1)
        self.assertEqual((self.root / kept[0]).read_text(encoding='utf-8'), '{"list": [ truncated')
        self.assertEqual(len(load_jobs(str(listing))), 1)

    def test_reading_never_moves_anything(self):
        listing = self.root / 'jobs.json'
        listing.write_text('not json', encoding='utf-8')
        with self.assertLogs('applicant.files', 'WARNING'):
            self.assertEqual(load_jobs(str(listing)), [])
        self.assertEqual(self.leftovers('jobs.json'), [])

    def test_a_json_list_instead_of_an_object_is_not_a_crash(self):
        listing = self.root / 'jobs.json'
        listing.write_text('[1, 2]', encoding='utf-8')
        with self.assertLogs('applicant.files', 'WARNING'):
            self.assertEqual(load_jobs(str(listing)), [])

    def test_one_invalid_record_does_not_fail_the_file(self):
        listing = self.root / 'jobs.json'
        records = [{'source': 'indeed', 'id': '1', 'title': 'Dev'}, {'source': 'indeed'}]
        listing.write_text(json.dumps({'list': records}), encoding='utf-8')
        with self.assertLogs('applicant.storage', 'WARNING'):
            self.assertEqual([job.id for job in load_jobs(str(listing))], ['1'])


class AtomicWriteTest(TempDir):
    def test_a_failed_write_leaves_the_old_file_and_no_temp_file(self):
        target = self.root / 'data.json'
        write_document(target, {'kept': True})
        with self.assertRaises(TypeError):
            write_document(target, {'bad': object()})
        self.assertEqual(read_document(target), {'kept': True})
        self.assertEqual(self.leftovers('data.json'), [])

    def test_an_existing_files_permissions_are_kept(self):
        target = self.root / 'data.json'
        target.write_text('{}', encoding='utf-8')
        os.chmod(target, 0o644)
        write_document(target, {'a': 1})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_a_new_file_is_owner_only(self):
        target = self.root / 'new.json'
        write_document(target, {})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


class PppTableTest(TempDir):
    def test_an_unreadable_table_is_kept_not_replaced_by_one_fetch(self):
        table = self.root / 'ppp_factors.json'
        table.write_text('{"factors": {"IND": ', encoding='utf-8')
        client = mock_client(
            lambda request: httpx.Response(200, json=[{}, [{'value': 1.5, 'date': '2025'}]])
        )
        with self.assertLogs('applicant.files', 'WARNING'):
            refresh_factors(path=table, currencies=['GBP'], pause=0, client=client)
        self.assertEqual(len([n for n in self.leftovers(table.name) if '.corrupt-' in n]), 1)


class TrackerStorageTest(TempDir):
    def financials(self) -> CompanyFinancials:
        return CompanyFinancials(
            source='crunchbase',
            company='Zomato',
            company_id='zomato',
            total_funding=Money(amount=1e9, currency='USD'),
        )

    def test_an_unreadable_history_is_moved_aside(self):
        history = self.root / 'company_financials.json'
        history.write_text('{"companies": {', encoding='utf-8')
        with self.assertLogs('applicant.files', 'WARNING'):
            FinancialsTracker(str(history)).record(self.financials())
        self.assertEqual(len([n for n in self.leftovers(history.name) if '.corrupt-' in n]), 1)

    def test_an_invalid_stored_record_keeps_its_snapshots(self):
        history = self.root / 'company_financials.json'
        tracker = FinancialsTracker(str(history))
        tracker.record(self.financials())
        tracker.data['companies']['crunchbase:zomato']['latest'] = {'source': 'crunchbase'}
        tracker.save()

        with self.assertLogs('applicant.financials.tracker', 'WARNING'):
            changes = FinancialsTracker(str(history)).record(self.financials())
        self.assertEqual(changes, [])
        self.assertEqual(len(FinancialsTracker(str(history)).history('crunchbase:zomato')), 2)


if __name__ == '__main__':
    unittest.main()
