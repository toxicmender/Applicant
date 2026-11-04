"""Candidate judgments preserve history, reject stale writes and support safe retries."""

import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from applicant.cli import main
from applicant.domain.canonical import canonical_json
from applicant.domain.feedback import FeedbackDocument, FeedbackEvent, FeedbackInput
from applicant.errors import StoreError
from applicant.infra.store.feedback import FeedbackRepository
from applicant.infra.store.profiles import RevisionConflict
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.services.feedback import feedback_report, import_feedback
from tests import test_ranking_cli as creation
from tests.ranking_fixtures import NOW


class FeedbackTest(unittest.TestCase):
    def setUp(self):
        self.data = creation.RankingCliTest()
        self.data.setUp()
        self.addCleanup(self.data.doCleanups)
        self.created = self.data.service(versions_path=self.data.bundle(self.data.mixed_jobs()))
        self.path = self.data.database
        self.job = self.created.results[0].job_id

    def request(self, **changes):
        return FeedbackInput.model_validate(
            {
                'event_id': 'event-1',
                'run_id': self.created.ranking_run_id,
                'job_id': self.job,
                'expected_revision': 0,
                'decision': 'shortlist',
            }
            | changes
        )

    def save(self, *events, dry_run=False):
        return import_feedback(
            self.path, FeedbackDocument(events=events), recorded_at=NOW, dry_run=dry_run
        )

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = main(['--no-log-file', '--data-dir', str(self.data.root), *args])
            except SystemExit as error:
                code = error.code
        return code, out.getvalue(), err.getvalue()

    def test_contract_rejects_ambiguous_feedback(self):
        for change in (
            {'decision': 'dismiss'},
            {'expected_revision': True},
            {'reasons': ['other', 'other']},
            {'reasons': ['unknown']},
            {'importance': {'salary': 'more_important'}},
            {'decision': 'clear', 'notes': 'leftover'},
            {'notes': 'a' * 2001},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.request(**change)
        with self.assertRaises(ValueError):
            FeedbackEvent(
                sequence=1,
                request=self.request(),
                revision=2,
                job_version_id='v',
                profile_id='p',
                profile_revision=1,
                recorded_at=NOW,
            )

    def test_dry_run_retry_revision_clear_and_cutoff(self):
        request = self.request(importance={'compensation': 'more_important'})
        before = self.path.read_bytes()
        self.assertEqual(self.save(request, dry_run=True)[0].revision, 1)
        self.assertEqual(self.path.read_bytes(), before)
        first = self.save(request)[0]
        self.assertEqual(self.save(request)[0], first)
        second = self.save(
            self.request(
                event_id='event-2', expected_revision=1, decision='dismiss', reasons=('timing',)
            )
        )[0]
        self.save(self.request(event_id='event-3', expected_revision=2, decision='clear'))
        report = feedback_report(self.path, self.created.ranking_run_id)
        self.assertEqual(report['events'][0]['request']['decision'], 'clear')
        self.assertEqual(
            feedback_report(self.path, self.created.ranking_run_id, through=second.sequence)[
                'events'
            ][0]['revision'],
            2,
        )
        self.assertEqual(
            len(feedback_report(self.path, self.created.ranking_run_id, history=True)['events']), 3
        )
        with self.assertRaises(RevisionConflict):
            self.save(self.request(event_id='stale'))
        with self.assertRaises(RevisionConflict):
            self.save(self.request(decision='dismiss', reasons=('other',)))
        for cutoff in (-1, 999):
            with self.assertRaisesRegex(ValueError, 'cutoff'):
                feedback_report(self.path, self.created.ranking_run_id, through=cutoff)

    def test_invalid_batch_rolls_back_and_validates_before_writer(self):
        first = self.request()
        before = self.path.read_bytes()
        with mock.patch('applicant.services.feedback.Store', wraps=Store) as store:
            with self.assertRaises(ValueError):
                self.save(first, self.request(event_id='bad', job_id='absent'))
            store.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        forged = first.model_copy(update={'expected_revision': -1})
        with self.assertRaises(ValueError):
            self.save(forged)

    def test_history_key_integrity_immutability_and_foreign_keys(self):
        self.save(self.request())
        with Store(self.path) as store:
            repo = FeedbackRepository(store.db)
            with self.assertRaises(StoreError):
                repo.append(self.request(), recorded_at=NOW)
            for statement in (
                'DELETE FROM feedback_events',
                'UPDATE feedback_events SET revision=2',
                'INSERT OR REPLACE INTO feedback_events SELECT * FROM feedback_events',
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute(statement)
                store.db.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(
                    "INSERT INTO feedback_events VALUES (2,'bad','missing','j','v',1,'{}')"
                )
            store.db.rollback()
            store.db.execute('DROP TRIGGER immutable_feedback_update')
            store.db.execute("UPDATE feedback_events SET event_id='changed'")
            store.db.commit()
            with self.assertRaises(StoreError):
                repo.history(self.created.ranking_run_id)

    def test_concurrent_edits_have_one_winner(self):
        def edit(number):
            try:
                self.save(self.request(event_id=f'event-{number}'))
                return 'saved'
            except RevisionConflict:
                return 'stale'

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(pool.map(edit, (1, 2)), ('saved', 'stale'))

    def test_upgrade_from_schema_three_and_cli_options(self):
        old = self.data.root / 'old # 测试.db'
        with mock.patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:3]), Store(old):
            pass
        before = old.read_bytes()
        with self.assertRaisesRegex(StoreError, 'applicant db upgrade'):
            Store.open_readonly(old)
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(self.cli('db', 'upgrade', '--database', str(old))[0], 0)
        self.assertEqual(self.cli('db', 'upgrade', '--database', str(old))[0], 0)
        self.assertEqual(self.cli('db', 'upgrade', '--database', 'absent.db')[0], 4)
        self.assertEqual(
            self.cli('--store', 'files', 'db', 'upgrade', '--database', str(old))[0], 2
        )
        args = (
            'feedback',
            'record',
            '--run',
            self.created.ranking_run_id,
            '--job',
            self.job,
            '--event-id',
            'cli',
            '--expected-revision',
            '0',
            '--decision',
            'shortlist',
        )
        self.assertEqual(self.cli(*args, '--importance', 'compensation=more_important')[0], 0)
        self.assertEqual(
            self.cli('feedback', 'list', '--run', self.created.ranking_run_id, '--json')[0], 0
        )
        self.assertEqual(self.cli('feedback', 'list', '--run', 'missing')[0], 4)
        path = self.data.write(
            'feedback.json',
            FeedbackDocument(events=(self.request(event_id='imported', expected_revision=1),)),
        )
        code, out, _ = self.cli('feedback', 'import', '--from-file', str(path), '--dry-run')
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['dry_run'])
        for options in (
            ('record',),
            ('list',),
            ('import',),
            ('record', '--history'),
            ('import', '--from-file', str(path), '--run', 'x'),
            ('list', '--run', 'x', '--dry-run'),
        ):
            self.assertEqual(self.cli('feedback', *options)[0], 2)
        self.assertEqual(self.cli(*args, '--importance', 'bad')[0], 2)
        self.assertEqual(
            self.cli(
                *args,
                '--importance',
                'compensation=more_important',
                '--importance',
                'compensation=less_important',
            )[0],
            2,
        )
        self.assertEqual(
            self.cli('feedback', 'list', '--run', 'x', '--feedback-through', '-1')[0], 2
        )
        path.write_text('{"events":[],"events":[]}', encoding='utf-8')
        self.assertEqual(self.cli('feedback', 'import', '--from-file', str(path))[0], 2)
        self.assertEqual(self.cli('feedback', 'list', '--feedback-through', 'invalid')[0], 2)

    def test_recovery_rejects_corrupt_profile_and_revision(self):
        event = self.save(self.request())[0]
        with Store(self.path) as store:
            store.db.execute('DROP TRIGGER immutable_feedback_update')
            bad = event.model_copy(update={'profile_id': 'wrong'})
            store.db.execute('UPDATE feedback_events SET payload=?', (canonical_json(bad),))
            store.db.commit()
            with self.assertRaisesRegex(StoreError, 'history is inconsistent'):
                FeedbackRepository(store.db).history(self.created.ranking_run_id)
