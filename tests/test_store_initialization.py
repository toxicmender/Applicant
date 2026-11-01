"""Connection integrity and migration serialization for existing databases."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from applicant.errors import StoreError
from applicant.infra.store.sqlite import MIGRATIONS, Store


class InitializationTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'applicant.db'

    def test_each_connection_enforces_foreign_keys_and_wal(self):
        for _ in range(2):
            with Store(self.path) as store:
                self.assertEqual(store.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(store.db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
                self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
                store.db.execute('CREATE TABLE IF NOT EXISTS parent (id INTEGER PRIMARY KEY)')
                store.db.execute(
                    'CREATE TABLE IF NOT EXISTS child (id INTEGER REFERENCES parent(id))'
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute('INSERT INTO child VALUES (42)')

    def test_future_schema_is_rejected_and_connection_released(self):
        db = sqlite3.connect(self.path)
        db.execute(f'PRAGMA user_version = {len(MIGRATIONS) + 1}')
        db.close()
        with self.assertRaisesRegex(StoreError, 'newer than supported'):
            Store(self.path)
        # On Windows an open SQLite handle prevents this rename/deletion.
        renamed = self.path.with_suffix('.rejected')
        self.path.rename(renamed)
        renamed.unlink()

    def test_failed_upgrade_keeps_existing_data_and_version(self):
        with Store(self.path) as store, store.db:
            store.db.execute("INSERT INTO documents VALUES ('listing', 'digest')")
        broken = (*MIGRATIONS, ('CREATE TABLE half (id INTEGER)', 'INVALID SQL'))
        with (
            mock.patch('applicant.infra.store.sqlite.MIGRATIONS', broken),
            self.assertRaises(StoreError),
        ):
            Store(self.path)
        with Store(self.path) as store:
            self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS))
            self.assertEqual(
                store.db.execute('SELECT * FROM documents').fetchall(), [('listing', 'digest')]
            )
            self.assertEqual(
                store.db.execute("SELECT name FROM sqlite_master WHERE name = 'half'").fetchall(),
                [],
            )

    def test_concurrent_initializers_apply_each_upgrade_once(self):
        # Initialize WAL first to isolate migration locking from file creation.
        with Store(self.path):
            pass
        barrier = threading.Barrier(2)
        migrations = (*MIGRATIONS, ('CREATE TABLE added_once (id INTEGER)',))

        def initialize():
            barrier.wait(timeout=5)
            with Store(self.path) as store:
                return store.db.execute('PRAGMA user_version').fetchone()[0]

        with (
            mock.patch('applicant.infra.store.sqlite.MIGRATIONS', migrations),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [executor.submit(initialize) for _ in range(2)]
            self.assertEqual(
                [future.result(timeout=10) for future in futures], [len(migrations)] * 2
            )
