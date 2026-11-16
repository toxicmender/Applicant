import json
import tempfile
import unittest
from pathlib import Path

from applicant.domain.canonical import content_hash
from applicant.errors import ApplicationBlocked, ConfigError
from applicant.services.application_attempts import AttemptJournal
from applicant.services.attempt_import import merge


class AttemptImportTest(unittest.TestCase):
    def test_partial_nested_metadata_merges_regardless_of_input_order(self):
        first = {'state': 'uncertain', 'job': {'id': '21', 'company': 'Acme', 'title': None}}
        second = {'state': 'uncertain', 'job': {'id': '21', 'company': None, 'title': 'Engineer'}}
        sources = [('first', {'one': first}), ('second', {'one': second})]
        for batch in (sources, list(reversed(sources))):
            with self.subTest(batch=batch):
                journal = {'entries': {}}
                merge(journal, batch)
                self.assertEqual(journal['conflicts'], {})
                self.assertEqual(
                    journal['entries']['one']['job'],
                    {
                        'id': '21',
                        'company': 'Acme',
                        'title': 'Engineer',
                    },
                )

    def test_disagreeing_sources_quarantine_without_choosing_a_winner(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'applications.csv'
            session = root / 'session.json'
            journal = AttemptJournal(log, session)
            job = {'id': '21', 'url': 'https://www.linkedin.com/jobs/view/21/'}
            record = {
                'job': job,
                'job_key': content_hash(['linkedin', '21']),
                'account': str(session),
                'recorded_at': '2026-01-02T03:04:05+00:00',
                'state': 'uncertain',
            }
            first = root / 'first.attempts.json'
            first.write_text(json.dumps({'duplicate': record}), encoding='utf-8')
            second = root / 'second.attempts.json'
            second.write_text(
                json.dumps({'duplicate': {**record, 'state': 'submitted'}}), encoding='utf-8'
            )
            (root / '.half-written.attempts.json').write_bytes(b'{')
            journal.import_legacy([root])
            group = journal.conflicts()['duplicate']
            self.assertEqual(group['journal'], {})
            self.assertEqual(len(group['incoming']), 2)
            with self.assertRaises(ApplicationBlocked):
                journal.begin(job)
            with self.assertRaisesRegex(ConfigError, 'supply --digest'):
                journal.resolve_conflict('duplicate', 'incoming', None)
            chosen = next(
                key for key, item in group['incoming'].items() if item['state'] == 'submitted'
            )
            journal.resolve_conflict('duplicate', 'incoming', chosen)
            journal.import_legacy([second, first])
            self.assertEqual(journal.conflicts(), {})
            self.assertEqual(journal.detail('duplicate')['state'], 'submitted')

    def test_invalid_second_snapshot_leaves_journal_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            journal = AttemptJournal(root / 'apply.csv', root / 'session.json')
            first, second = root / '1.attempts.json', root / '2.attempts.json'
            first.write_bytes(b'{}')
            second.write_bytes(b'{')
            with self.assertRaises(ConfigError):
                journal.import_legacy([first, second])
            self.assertFalse(journal.path.exists())
