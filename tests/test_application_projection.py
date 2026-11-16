import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from applicant.domain.job import Job
from applicant.domain.ports import UNCONFIRMED
from applicant.errors import ExportPending
from applicant.infra.store.application_csv import ApplicationCSV
from applicant.infra.store.financials_lock import export_lock
from applicant.infra.store.repositories import applications
from applicant.services.application_attempts import AttemptJournal
from applicant.services.application_projection import publish_file
from applicant.services.status import summarise


class ApplicationProjectionTest(unittest.TestCase):
    def test_reconcile_updates_only_uncertain_row_in_both_backends(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'applications.csv'
                profile = root / 'session.json'
                job = Job(
                    source='linkedin',
                    id='31',
                    title='Engineer',
                    url='https://www.linkedin.com/jobs/view/31/',
                )
                journal = AttemptJournal(log, profile, backend=backend)
                identifier = journal.begin(job.to_dict())
                repo = applications(log, backend)
                repo.record([(job, 'needs_manual_apply', UNCONFIRMED)])
                journal.resolve(identifier, 'submitted')
                with export_lock(log):
                    found = summarise(str(root / 'jobs.json'), str(log), backend)
                self.assertTrue(found.projection_pending)
                self.assertEqual(found.by_status, {'needs_manual_apply': 1})
                found = summarise(str(root / 'jobs.json'), str(log), backend)
                self.assertEqual(found.by_status, {'applied': 1})
                self.assertFalse(found.projection_pending)
                self.assertIn(identifier, repo.rows()[0]['note'])

    def test_sqlite_receipt_retries_pending_file_before_acknowledging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'applications.csv'
            journal = AttemptJournal(log, root / 'session.json', backend='sqlite')
            job = Job(
                source='linkedin',
                id='31',
                title='Engineer',
                url='https://www.linkedin.com/jobs/view/31/',
            )
            identifier = journal.begin(job.to_dict())
            journal.resolve(identifier, 'submitted')
            with (
                patch(
                    'applicant.infra.store.sqlite.write_document_bytes', side_effect=ExportPending
                ),
                self.assertRaises(ExportPending),
            ):
                journal.recover_confirmed()
            self.assertFalse(log.exists())
            journal.recover_confirmed()
            self.assertEqual(len(ApplicationCSV(log.read_bytes()).rows()), 1)
            journal.recover_confirmed()
            self.assertEqual(len(ApplicationCSV(log.read_bytes()).rows()), 1)

    def test_csv_keeps_empty_cells_and_rejects_ragged_or_mixed_records(self):
        raw = b'"source","id","status","note",""\n"linkedin","31","failed","x,""y""",""\n'
        self.assertEqual(ApplicationCSV(raw).render(), raw)
        for row in (b'linkedin,31,failed\n', b'linkedin,31,failed,note\r\n'):
            with self.assertRaises(ValueError):
                ApplicationCSV(b'source,id,status,note\n' + row)

    def test_file_acknowledges_unchanged_tag_after_an_unrelated_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'applications.csv'
            job = Job(source='linkedin', id='31', title='Engineer')
            event = {'attempt': 'sample', 'entry': {'job': job.to_dict(), 'state': 'submitted'}}
            publish_file(path, event, lambda: None)
            layout = ApplicationCSV(path.read_bytes())
            layout.set(None, {'source': 'indeed', 'id': '2', 'status': 'failed'})
            edited = layout.render()
            path.write_bytes(edited)
            publish_file(path, event, lambda: self.fail('existing intent was overwritten'))
            self.assertEqual(path.read_bytes(), edited)
