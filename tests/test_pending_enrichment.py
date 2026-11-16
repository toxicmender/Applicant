import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.cli import main
from applicant.domain.job import Job
from applicant.infra.store.repositories import listing
from applicant.services.application_attempts import AttemptJournal
from applicant.services.company_cache import CacheClock
from applicant.services.description_cache import DescriptionCache
from tests.fakes import FakeBoard


class DescriptionBoard(FakeBoard):
    describe: Mock


class PendingEnrichmentTest(unittest.TestCase):
    def test_resume_promotes_interrupted_postings_in_both_backends(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / 'listings.json'
                job = Job(
                    source='linkedin',
                    id='21',
                    title='Engineer',
                    url='https://www.linkedin.com/jobs/view/21/',
                )
                interrupted = DescriptionBoard([job])
                interrupted.describe = Mock(side_effect=KeyboardInterrupt)
                resumed = DescriptionBoard([])
                resumed.describe = Mock(return_value='3+ years of experience')
                prefix = ['--no-log-file', '--store', backend, 'search']
                flags = ['--source', 'linkedin', '--output', str(output)]
                with (
                    patch('applicant.search.Jobs._client', side_effect=[interrupted, resumed]),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        main(
                            [
                                *prefix,
                                'Engineer',
                                *flags,
                                '--experience',
                                '5',
                                '--enrich',
                                '--enrich-limit',
                                '1',
                            ]
                        ),
                        130,
                    )
                    self.assertEqual(listing(output, backend).load(), [])
                    self.assertEqual(main([*prefix, '--resume-pending', *flags]), 0)
                    self.assertEqual(listing(output, backend).load()[0].experience_min, 3)
                resumed.describe.assert_called_once()

                from applicant.boards.linkedin_apply import LinkedIn
                from applicant.infra.store.repositories import applications

                log = output.with_name('applied.csv')
                session = output.with_name('session.json')
                journal = AttemptJournal(log, session, backend=backend)
                adapter = LinkedIn(delay=0)
                self.addCleanup(adapter.close)
                dispatch = Mock(side_effect=TimeoutError('no receipt'))
                args = [
                    '--no-log-file',
                    '--store',
                    backend,
                    'apply',
                    '--input',
                    str(output),
                    '--log',
                    str(log),
                    '--cookies',
                    str(session),
                ]
                with (
                    patch('applicant.search.Jobs.linkedin', return_value=adapter),
                    patch.object(adapter, '_start', return_value=Mock(url=job.url)),
                    patch('applicant.boards.linkedin_apply.looks_blocked', return_value=False),
                    patch('applicant.boards.linkedin_apply._prepare_submit', return_value=dispatch),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(main([*args, '--yes']), 0)
                    attempt = next(iter(json.loads(journal.path.read_bytes())['entries']))
                    self.assertEqual(journal.detail(attempt)['state'], 'uncertain')
                    self.assertEqual(
                        main([*args, '--reconcile', attempt, '--outcome', 'submitted']), 0
                    )
                self.assertEqual(
                    [row['status'] for row in applications(log, backend).rows()], ['applied']
                )
                dispatch.assert_called_once()

    def test_forward_jump_does_not_advance_persisted_watermark(self):
        at = datetime(2026, 11, 1, tzinfo=timezone.utc)
        wall, mono = Mock(return_value=at), Mock(return_value=0)
        job = Job(source='linkedin', id='21', title='Engineer')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.json'
            cache = DescriptionCache(path, clock=CacheClock(wall, mono))
            cache.write(job, 'original')
            mono.return_value = 1
            wall.return_value = at + timedelta(minutes=14)
            cache.write(job, 'during spike')
            wall.return_value = at - timedelta(minutes=2)
            restarted = DescriptionCache(path, clock=CacheClock(wall, mono))
            self.assertEqual(restarted.read(job), (True, 'original'))
