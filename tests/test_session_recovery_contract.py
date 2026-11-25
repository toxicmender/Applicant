"""A failed login attempt leaves the installed credential usable."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.infra.store.sqlite import Store
from applicant.services.provider_health import health
from applicant.services.provider_setup import (
    clear_session,
    provider_session,
    session_path,
    session_status,
    setup_provider,
)
from applicant.settings import Settings
from tests.test_setup_lifecycle import STATE


class SessionRecoveryContractTest(unittest.TestCase):
    def test_cancelled_reauthentication_preserves_installed_state_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder, patch('sys.stdin.isatty', return_value=True):
            settings = Settings(session_storage='plaintext', data_dir=Path(folder), store='files')
            verify = Mock(return_value=STATE)
            setup_provider(settings, 'linkedin', verifier=verify)
            target = session_path(settings, 'linkedin')
            before = target.read_bytes()
            with self.assertRaises(KeyboardInterrupt):
                setup_provider(
                    settings, 'linkedin', reauth=True, verifier=Mock(side_effect=KeyboardInterrupt)
                )
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'verified')
            self.assertEqual(
                setup_provider(settings, 'linkedin', reauth=True, verifier=verify)['status'],
                'verified',
            )

    def test_file_only_check_retains_busy_sqlite_recovery(self):
        with tempfile.TemporaryDirectory() as folder, patch('sys.stdin.isatty', return_value=True):
            root = Path(folder)
            database = root / 'selected.db'
            sqlite = Settings(data_dir=root, store='sqlite', session_storage='plaintext')
            files = sqlite.model_copy(update={'store': 'files'})
            with Store(database) as db, db.db:
                db.db.execute(
                    "INSERT INTO provider_health(provider,status) VALUES ('linkedin','needs_setup')"
                )
            store = provider_session(sqlite, 'linkedin')
            with closing(sqlite3.connect(database)) as blocker:
                blocker.execute('BEGIN IMMEDIATE')
                saved = setup_provider(
                    sqlite, 'linkedin', database=str(database), verifier=Mock(return_value=STATE)
                )
                self.assertEqual(saved['status'], 'verified')
                recovery = saved['health_recovery']
                assert isinstance(recovery, dict)
                self.assertEqual(recovery['status'], 'database_busy')
                pending = store.snapshot()
                checked = session_status(
                    files, 'linkedin', check=True, verifier=Mock(side_effect=AssertionError)
                )
                self.assertEqual(checked['status'], 'verified')
                self.assertEqual(
                    store.snapshot(), pending, 'file-only status discarded SQLite recovery'
                )
            checked = session_status(
                sqlite, 'linkedin', check=True, verifier=Mock(side_effect=AssertionError)
            )
            recovery = checked['health_recovery']
            assert isinstance(recovery, dict)
            self.assertEqual(recovery['status'], 'recovered')
            self.assertEqual(health(database, 'linkedin')['status'], 'ready')
            self.assertIsNone(store.snapshot()['pending'])
            self.assertEqual(store.snapshot()['generation'], pending['generation'])

    def test_clear_replaces_pending_recovery_and_preserves_chromium_files(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(session_storage='plaintext', data_dir=Path(folder), store='files')
            store = provider_session(settings, 'glassdoor')
            store.path.parent.mkdir()
            sentinel = store.path.parent / 'SingletonLock'
            sentinel.write_text('chromium-fixture', encoding='utf-8')
            previous = store.snapshot()
            store.install(previous, STATE, 'verified', pending='recover')
            with patch(
                'applicant.infra.session_store.SessionStore.reconcile',
                return_value=({}, {'status': 'database_busy', 'retryable': True}),
            ):
                clear_session(settings, 'glassdoor')
            calls = []

            def reconcile(action, _database):
                calls.append(action)
                return {'status': 'recovered'}

            record, _ = store.reconcile(reconcile)
            self.assertEqual(calls, ['clear'])
            self.assertEqual(record['status'], 'cleared')
            self.assertIsNone(store.credentials(record))
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'chromium-fixture')
