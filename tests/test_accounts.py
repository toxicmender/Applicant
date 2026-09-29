"""Acting on the user's account: kept apart, never implicit, always confirmed.

Reading job cards and submitting applications in someone's name are different
risks. The code that can do the second lives in `applicant.boards.linkedin_apply`,
a search never loads it, a dry run never loads it, and the command line asks
before anything is sent.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from applicant.boards import linkedin as guest_module
from applicant.boards.linkedin import LinkedInGuest
from applicant.boards.linkedin_apply import LinkedIn
from applicant.cli import main
from applicant.domain.job import Job
from applicant.domain.ports import ApplicationResult
from applicant.errors import Blocked
from applicant.search import Jobs
from applicant.services.apply import ApplyToJobs, EasyApply
from applicant.storage import ApplicationLog, save_jobs

ROOT = Path(__file__).resolve().parent.parent


def posting(source: str, job_id: str, title: str = 'Dev') -> Job:
    return Job(
        source=source,
        id=job_id,
        title=title,
        company=f'{source} co',
        url=f'https://{source}.example/{job_id}',
    )


class TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)


def loaded_after(script: str) -> set[str]:
    """The applicant modules a fresh interpreter has loaded after `script`."""
    probe = textwrap.dedent(script) + textwrap.dedent(
        """
        import sys
        print('\\n'.join(name for name in sys.modules if name.startswith('applicant')))
        """
    )
    result = subprocess.run(
        [sys.executable, '-c', probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')},
        timeout=120,
        check=True,
    )
    return set(result.stdout.split())


# the network, answered with an empty page: a guest search that finds nothing
NO_NETWORK = """
import httpx
from applicant.infra.http import HttpClient
HttpClient.request = lambda self, method, url, **kw: httpx.Response(200, text='')
"""


class SeparationTest(TempDir):
    """The phase's acceptance test: search never imports the apply module."""

    ACCOUNT = 'applicant.boards.linkedin_apply'

    def test_a_linkedin_search_never_loads_the_account_code(self):
        loaded = loaded_after(
            NO_NETWORK
            + f"""
from applicant.cli import main
main(['search', 'python', '-s', 'linkedin', '--store', 'files', '--no-log-file',
      '-o', {str(self.root / 'jobs.json')!r}])
"""
        )
        self.assertIn('applicant.boards.linkedin', loaded, 'the guest search did run')
        self.assertNotIn(self.ACCOUNT, loaded)

    def test_a_dry_run_never_loads_it_either(self):
        listing = self.root / 'jobs.json'
        save_jobs([posting('linkedin', '1')], str(listing))
        loaded = loaded_after(
            f"""
from applicant.cli import main
main(['apply', '-i', {str(listing)!r}, '--log', {str(self.root / 'log.csv')!r},
      '--dry-run', '--store', 'files', '--no-log-file'])
"""
        )
        self.assertIn('applicant.services.apply', loaded)
        self.assertNotIn(self.ACCOUNT, loaded)

    def test_the_guest_client_cannot_act_as_you(self):
        for action in ('login', 'restore_session', 'scrape_jobs', 'easy_apply', 'apply'):
            with self.subTest(action=action):
                self.assertFalse(hasattr(LinkedInGuest, action))

    def test_the_account_client_is_only_where_it_lives(self):
        """The lazy `boards.linkedin.LinkedIn` of 0.1.x is gone."""
        self.assertFalse(hasattr(guest_module, 'LinkedIn'))
        self.assertTrue(issubclass(LinkedIn, LinkedInGuest))


class ApplierPortTest(unittest.TestCase):
    def test_dry_run_has_no_default(self):
        with self.assertRaises(TypeError):
            LinkedIn(delay=0).apply([posting('linkedin', '1')])  # type: ignore[call-arg]

    def test_a_dry_run_touches_nothing(self):
        client = LinkedIn(delay=0)
        with mock.patch.object(LinkedIn, '_start', side_effect=AssertionError('a browser!')):
            results = client.apply([posting('linkedin', '1')], dry_run=True)
        self.assertEqual([result.status for result in results], ['would_apply'])

    def test_a_real_run_reports_each_job(self):
        jobs = [posting('linkedin', '1'), posting('linkedin', '2')]
        client = LinkedIn(delay=0)
        with mock.patch.object(LinkedIn, 'easy_apply', return_value=[jobs[0].url]):
            results = client.apply(jobs, dry_run=False)
        self.assertEqual([result.status for result in results], ['applied', 'needs_manual_apply'])


class Recorder:
    """An Applier that records what it was asked to do."""

    source = 'linkedin'

    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[list[Job], bool]] = []

    def apply(self, jobs, *, dry_run):
        self.calls.append((jobs, dry_run))
        if self.error:
            raise self.error
        return [ApplicationResult(job, 'applied', 'ok') for job in jobs]


class ApplyServiceTest(TempDir):
    def run_service(self, applier, confirm=None, dry_run=False):
        log = str(self.root / 'applied.csv')
        service = ApplyToJobs({'linkedin': applier}, confirm=confirm)
        jobs = [posting('linkedin', '1'), posting('indeed', '2')]
        with self.assertNoLogs('applicant', 'ERROR'):
            service.run(jobs, log=log, dry_run=dry_run)
        return service, {row['id']: row['status'] for row in ApplicationLog(log).rows()}

    def test_dry_run_is_required(self):
        with self.assertRaises(TypeError):
            ApplyToJobs({}).run([])  # type: ignore[call-arg]

    def test_declining_submits_nothing_and_logs_nothing_for_it(self):
        applier = Recorder()
        service, statuses = self.run_service(applier, confirm=lambda targets: False)
        self.assertEqual(applier.calls, [])
        self.assertEqual(statuses, {'2': 'needs_manual_apply'}, 'a later run can still apply')
        self.assertEqual([job.id for job in service.declined], ['1'])

    def test_confirming_submits(self):
        asked = []
        applier = Recorder()
        _, statuses = self.run_service(
            applier, confirm=lambda targets: asked.append(targets) or True
        )
        self.assertEqual([job.id for job in asked[0]], ['1'], 'shown exactly what is sent')
        self.assertEqual(statuses['1'], 'applied')

    def test_a_dry_run_asks_no_one(self):
        applier = Recorder()
        self.run_service(applier, confirm=lambda targets: self.fail('asked'), dry_run=True)
        self.assertEqual(applier.calls[0][1], True)

    def test_a_failing_applier_is_a_failed_row_not_a_lost_run(self):
        with self.assertLogs('applicant.services.apply', 'WARNING'):
            _, statuses = self.run_service_quietly(Recorder(Blocked('checkpoint')))
        self.assertEqual(statuses, {'1': 'failed', '2': 'needs_manual_apply'})

    def run_service_quietly(self, applier):
        log = str(self.root / 'applied.csv')
        ApplyToJobs({'linkedin': applier}).run(
            [posting('linkedin', '1'), posting('indeed', '2')], log=log, dry_run=False
        )
        return None, {row['id']: row['status'] for row in ApplicationLog(log).rows()}

    def test_the_easy_apply_adapter_makes_no_client_for_a_dry_run(self):
        made = mock.Mock(side_effect=AssertionError('client made'))
        results = EasyApply(made).apply([posting('linkedin', '1')], dry_run=True)
        self.assertEqual(results[0].status, 'would_apply')

    def test_the_facade_needs_dry_run_too(self):
        """It warned in 0.1.x; since 0.2.0 leaving it out is an error."""
        with self.assertRaises(TypeError):
            Jobs().apply([], log=str(self.root / 'log.csv'))  # type: ignore[call-arg]


class ConfirmCommandTest(TempDir):
    def setUp(self):
        super().setUp()
        self.listing = str(self.root / 'jobs.json')
        self.log = str(self.root / 'applied.csv')
        save_jobs([posting('linkedin', '1', 'Python Dev'), posting('indeed', '2')], self.listing)

    def run_apply(self, *extra) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = main(['apply', '-i', self.listing, '--log', self.log, '--no-log-file', *extra])
        return code, out.getvalue()

    def test_with_no_terminal_nothing_is_submitted(self):
        with (
            mock.patch('sys.stdin.isatty', return_value=False),
            mock.patch.object(LinkedIn, 'easy_apply', side_effect=AssertionError('submitted!')),
        ):
            code, out = self.run_apply()
        self.assertEqual(code, 0)
        self.assertIn('about to submit 1 application(s)', out)
        self.assertIn('Python Dev', out, 'shown exactly what would be sent')
        self.assertIn('nothing submitted', out)
        statuses = {row['id']: row['status'] for row in ApplicationLog(self.log).rows()}
        self.assertEqual(statuses, {'2': 'needs_manual_apply'})

    def test_an_answer_other_than_yes_is_no(self):
        with (
            mock.patch('sys.stdin.isatty', return_value=True),
            mock.patch('applicant.cli.apply.terminal') as terminal,
            mock.patch.object(LinkedIn, 'easy_apply', side_effect=AssertionError('submitted!')),
        ):
            terminal.return_value.ask.return_value = 'y'
            _, out = self.run_apply()
        self.assertIn('not submitted: 1', out)

    def test_yes_submits_without_asking(self):
        with mock.patch.object(LinkedIn, 'easy_apply', return_value=['https://linkedin.example/1']):
            code, out = self.run_apply('--yes')
        self.assertEqual(code, 0)
        self.assertNotIn('about to submit', out)
        statuses = {row['id']: row['status'] for row in ApplicationLog(self.log).rows()}
        self.assertEqual(statuses['1'], 'applied')


POSTING = {'source': 'linkedin', 'url': 'https://www.linkedin.com/jobs/view/7', 'title': 'ML'}


class ConfirmedApplicationTest(unittest.TestCase):
    """A click on Submit is not an application until LinkedIn says it is."""

    def page(self, *, sent: bool, timed_out: bool = False) -> mock.MagicMock:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        from applicant.boards.linkedin_apply import APPLICATION_SENT

        page = mock.MagicMock()
        located: dict[str, mock.MagicMock] = {}

        def locator(selector):
            if selector not in located:
                found = mock.MagicMock(name=selector)
                found.count.return_value = 1
                found.first.count.return_value = 1
                located[selector] = found
            return located[selector]

        page.locator.side_effect = locator
        locator('#follow-company-checkbox').count.return_value = 0
        sent_locator = locator(APPLICATION_SENT)
        sent_locator.count.return_value = 1 if sent else 0
        either = sent_locator.or_.return_value.first
        if timed_out:
            either.wait_for.side_effect = PlaywrightTimeout('no answer')
        return page

    def test_a_confirmed_application_counts(self):
        page = self.page(sent=True)
        self.assertTrue(LinkedIn(delay=0)._apply_one(page, POSTING))

    def test_a_form_error_does_not_count_and_the_form_is_left_open(self):
        page = self.page(sent=False)
        self.assertFalse(LinkedIn(delay=0)._apply_one(page, POSTING))
        page.keyboard.press.assert_called_with('Escape')

    def test_no_answer_at_all_does_not_count(self):
        page = self.page(sent=False, timed_out=True)
        self.assertFalse(LinkedIn(delay=0)._apply_one(page, POSTING))


class ScrapeJobsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def card(self, job_id: str, text: str, *, once: bool = False) -> mock.MagicMock:
        """A recommended-job card; `once` makes it leave the DOM after one read."""
        card = mock.MagicMock()
        if once:
            card.inner_text.side_effect = [text, RuntimeError('element detached')]
        else:
            card.inner_text.return_value = text
        card.get_attribute.return_value = job_id
        link = card.locator.return_value.first
        link.count.return_value = 1
        link.get_attribute.return_value = f'/jobs/view/{job_id}/?trk=x'
        return card

    def scrape(self, backend) -> list[Job]:
        cards = [
            self.card('1', 'ML Engineer\nAcme\nPune\nEasy Apply', once=True),
            self.card('2', 'Data Scientist\nBeta\nBengaluru'),
        ]
        page = mock.MagicMock()
        page.url = 'https://www.linkedin.com/jobs/collections/recommended/'
        listed = page.locator.return_value
        listed.count.return_value = len(cards)
        listed.nth.side_effect = lambda index: cards[index]
        with mock.patch.object(LinkedIn, '_start', return_value=page):
            return LinkedIn(delay=0).scrape_jobs(str(self.root / 'jobs.json'), backend=backend)

    def test_a_card_that_leaves_the_dom_does_not_end_the_scrape(self):
        jobs = self.scrape('files')
        self.assertEqual([job.id for job in jobs], ['1', '2'])
        self.assertEqual([job.easy_apply for job in jobs], [True, False])
        self.assertEqual(jobs[0].url, 'https://www.linkedin.com/jobs/view/1/')

    def test_the_jobs_go_through_the_store_asked_for(self):
        from applicant.infra.store.sqlite import DB_NAME, Store

        self.scrape('sqlite')
        self.assertTrue((self.root / DB_NAME).exists())
        # straight from the table: a read through the store would import the
        # file, and pass whether or not the scrape wrote to the database
        with Store(self.root / DB_NAME) as store:
            rows = store.db.execute('SELECT payload FROM jobs ORDER BY seq').fetchall()
        self.assertEqual([json.loads(payload)['id'] for (payload,) in rows], ['1', '2'])


if __name__ == '__main__':
    unittest.main()
