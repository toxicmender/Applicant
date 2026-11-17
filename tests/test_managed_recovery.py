import errno
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from applicant.cli import main
from applicant.errors import ExportConflict, StoreError
from applicant.infra.store.application_csv import ApplicationCSV
from applicant.infra.store.export_resolution import resolve_export
from applicant.infra.store.managed_merge import parse_applications
from applicant.infra.store.repositories import applications, listing
from applicant.infra.store.sqlite import DB_NAME, Store
from applicant.services.application_attempts import AttemptJournal
from applicant.services.status import summarise
from applicant.storage import application_row, write_rows
from tests.test_store import JOB


class ManagedRecoveryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)

    def resolve(self, target, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(
                [
                    '--no-log-file',
                    'store',
                    'resolve-export',
                    '--database',
                    str(self.root / DB_NAME),
                    '--export',
                    str(target),
                    '--use',
                    'file',
                    *args,
                ]
            )
        self.assertTrue(stdout.getvalue(), stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        assert isinstance(payload, dict)
        return code, payload

    def test_listing_recovery_keeps_disjoint_changes_and_can_accept_conflicting_file(self):
        target = self.root / 'jobs.json'
        repo = listing(target, 'sqlite')
        other = JOB.model_copy(update={'id': 'manual', 'url': 'https://example.com/manual'})
        repo.save([JOB])
        updated = JOB.model_copy(update={'title': 'Changed in SQLite'})
        with (
            patch('applicant.infra.store.sqlite.write_document_bytes', side_effect=OSError),
            self.assertRaises(StoreError),
        ):
            repo.save([updated])
        target.write_text(json.dumps({'list': [JOB.to_dict(), other.to_dict()], 'note': 'manual'}))
        self.assertEqual(
            {job.id: job.title for job in repo.load()},
            {
                JOB.id: 'Changed in SQLite',
                'manual': other.title,
            },
        )
        self.assertEqual(json.loads(target.read_bytes())['note'], 'manual')

        with (
            patch('applicant.infra.store.sqlite.write_document_bytes', side_effect=OSError),
            self.assertRaises(StoreError),
        ):
            repo.save([JOB])
        manual = json.loads(target.read_bytes())
        manual['list'][0]['title'] = 'Changed on disk'
        target.write_text(json.dumps(manual))
        status, preview = self.resolve(target, '--dry-run')
        self.assertEqual(status, 0)
        status, result = self.resolve(target, '--confirm', preview['token'])
        self.assertEqual(status, 0)
        self.assertEqual(result['status'], 'resolved')
        self.assertEqual(
            json.loads(Path(result['backup']).read_bytes())['list'][0]['title'], JOB.title
        )
        self.assertIn('Changed on disk', [j.title for j in repo.load()])

    def write_csv(self, target, rows):
        with target.open('w', encoding='utf-8-sig', newline='') as handle:
            write_rows(handle, rows, header=True)

    def test_application_recovery_merges_disjoint_appends_and_keeps_journal(self):
        target = self.root / 'applied.csv'
        repo = applications(target, 'sqlite')
        repo.record([(JOB, 'applied', 'confirmed')])
        baseline = parse_applications(target.read_bytes())
        second = JOB.model_copy(update={'id': '2', 'url': 'https://example.com/jobs/2'})
        third = JOB.model_copy(update={'id': '3', 'url': 'https://example.com/jobs/3'})
        journal = AttemptJournal(target, self.root / 'session.json', backend='sqlite')
        attempt = journal.begin(second.to_dict())
        with (
            patch('applicant.infra.store.sqlite.write_document_bytes', side_effect=OSError),
            self.assertRaises(StoreError),
        ):
            repo.record([(second, 'would_apply', 'dry run')])
        self.write_csv(target, [*baseline, application_row(third, 'would_apply', 'manual')])
        self.assertEqual([row['id'] for row in repo.rows()], [str(JOB.id), '2', '3'])
        self.assertEqual(journal.detail(attempt)['state'], 'uncertain')

    def test_accepting_conflicting_rating_preserves_prior_observations(self):
        from applicant.reviews import AmbitionBoxClient
        from applicant.reviews.models import CompanyRating
        from applicant.services.reviews import fetch_reviews

        target = self.root / 'rating.json'
        client = AmbitionBoxClient()
        self.addCleanup(client.close)
        first = CompanyRating(
            'ambitionbox',
            'Acme',
            'https://www.ambitionbox.com/reviews/acme-reviews',
            '1',
            overall_rating=3.0,
        )
        with patch.object(client, 'fetch', return_value=first):
            fetch_reviews('Acme', {'ambitionbox': client}, str(target), backend='sqlite')
            first.overall_rating = 4.0
            with (
                patch('applicant.infra.store.sqlite.write_document_bytes', side_effect=OSError),
                self.assertRaises(StoreError),
            ):
                fetch_reviews('Acme', {'ambitionbox': client}, str(target), backend='sqlite')
        first.overall_rating = 4.5
        target.write_text(json.dumps(first.to_dict()))
        code, preview = self.resolve(target, '--dry-run')
        self.assertEqual(code, 0)
        code, result = self.resolve(target, '--confirm', preview['token'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(Path(result['backup']).read_bytes())['overall_rating'], 4.0)
        self.assertEqual(json.loads(target.read_bytes())['overall_rating'], 4.5)
        with Store.open_readonly(self.root / DB_NAME) as store:
            ratings = store.rating_history('Acme')
        self.assertEqual(sorted(rating['overall_rating'] for rating in ratings), [3.0, 4.0, 4.5])

    def test_snapshot_preserves_positions_and_restores_missing_or_corrupt_export(self):
        target = self.root / 'positions.csv'
        repo = applications(target, 'sqlite')
        original = b'id,status,source,note,custom,,custom,\n1,applied,linkedin,x,a,b,c,d\n'
        target.write_bytes(original)
        self.assertEqual(repo.rows()[0]['id'], '1')
        repo.record([(JOB.model_copy(update={'id': '2'}), 'applied', 'new')])
        intended = target.read_bytes()
        self.assertEqual(ApplicationCSV(intended).cells[0], ApplicationCSV(original).cells[0])
        target.unlink()
        self.assertEqual([row['id'] for row in repo.rows()], ['1', '2'])
        self.assertEqual(target.read_bytes(), intended)
        corrupt = b'id,status,source,note\n"torn'
        target.write_bytes(corrupt)
        with self.assertRaises(ExportConflict):
            repo.rows()
        database = self.root / DB_NAME
        preview = resolve_export(database, target, 'sqlite')
        self.assertEqual(preview['source'], 'snapshot')
        self.assertEqual(target.read_bytes(), corrupt)
        with self.assertRaises(ExportConflict):
            resolve_export(database, target, 'file')
        restored = resolve_export(database, target, 'sqlite', preview['token'])
        self.assertEqual(Path(restored['backup']).read_bytes(), corrupt)
        self.assertEqual(target.read_bytes(), intended)

    def test_publication_rechecks_external_drift_after_transient_failure(self):
        from applicant.files import write_document_bytes

        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                target = self.root / f'pending-{conflict}.csv'
                repo = applications(target, 'sqlite')
                original = b'id,status,source,note,custom,\n1,applied,linkedin,x,a,b\n'
                target.write_bytes(original)
                existing = original.replace(b',a,b', b',changed,b') if conflict else original
                edited = existing + b'3,applied,linkedin,external, =formula,\n'

                def block_once(path, content, *, target=target, edited=edited, **kwargs):
                    target.write_bytes(edited)
                    publisher.side_effect = write_document_bytes
                    raise BlockingIOError(errno.EAGAIN, 'temporary open handle')

                with patch('applicant.infra.store.sqlite.write_document_bytes') as publisher:
                    publisher.side_effect = block_once
                    entry = (JOB.model_copy(update={'id': '2'}), 'applied', 'new')
                    if conflict:
                        with self.assertRaises(ExportConflict):
                            repo.record([entry])
                        self.assertEqual(target.read_bytes(), edited)
                    else:
                        self.assertEqual(repo.record([entry]), 1)
                        layout = ApplicationCSV(target.read_bytes())
                        self.assertEqual([row[0] for row in layout.cells], ['1', '2', '3'])
                        self.assertEqual(layout.cells[-1][4], "' =formula")
                        self.assertEqual(len(repo.rows()), 3)

    def test_status_uses_committed_rows_after_one_attempt_then_normal_read_recovers(self):

        target = self.root / 'deferred.csv'
        repo = applications(target, 'sqlite')
        with (
            patch('applicant.infra.store.sqlite.write_document_bytes', side_effect=OSError),
            self.assertRaises(StoreError),
        ):
            repo.record([(JOB, 'applied', 'new')])
        self.assertFalse(target.exists())
        with patch(
            'applicant.infra.store.sqlite.write_document_bytes',
            side_effect=BlockingIOError(errno.EAGAIN, 'busy'),
        ) as publisher:
            summary = summarise(str(self.root / 'unused.json'), str(target), 'sqlite')
            self.assertTrue(summary.export_pending)
            self.assertEqual(summary.by_status, {'applied': 1})
            publisher.assert_called_once()
        self.assertEqual(len(repo.rows()), 1)
        self.assertTrue(target.is_file())

    def test_accepting_application_file_cannot_discard_settled_rows(self):
        target = self.root / 'applied.csv'
        repo = applications(target, 'sqlite')
        repo.record([(JOB, 'applied', 'confirmed')])
        baseline = parse_applications(target.read_bytes())
        second = JOB.model_copy(update={'id': '2', 'url': 'https://example.com/jobs/2'})
        journal = AttemptJournal(target, self.root / 'session.json', backend='sqlite')
        attempt = journal.begin(second.to_dict())
        with (
            patch('applicant.infra.store.sqlite.write_document_bytes', side_effect=OSError),
            self.assertRaises(StoreError),
        ):
            repo.record([(second, 'would_apply', 'dry run')])
        self.write_csv(target, [application_row(second, 'needs_manual_apply', 'manual')])
        code, message = self.resolve(target, '--dry-run')
        self.assertEqual(code, 1)
        self.assertEqual(message['status'], 'export_conflict')
        self.assertIn('settled application outcomes', message['message'])
        self.assertEqual(parse_applications(target.read_bytes())[0]['id'], '2')

        self.write_csv(target, [*baseline, application_row(second, 'needs_manual_apply', 'manual')])
        code, preview = self.resolve(target, '--dry-run')
        self.assertEqual(code, 0)
        code, result = self.resolve(target, '--confirm', preview['token'])
        self.assertEqual(code, 0)
        self.assertEqual(
            parse_applications(Path(result['backup']).read_bytes())[-1]['status'], 'would_apply'
        )
        self.assertEqual([row['status'] for row in repo.rows()], ['applied', 'needs_manual_apply'])
        self.assertEqual(journal.detail(attempt)['state'], 'uncertain')
        with Store.open_readonly(self.root / DB_NAME) as store:
            self.assertEqual(
                store.db.execute('SELECT COUNT(*) FROM pending_exports').fetchone(), (0,)
            )
