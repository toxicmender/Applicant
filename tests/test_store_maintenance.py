"""Maintenance respects readers and never initializes an absent database."""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from applicant.errors import StoreError
from applicant.infra.store.maintenance import checkpoint
from applicant.infra.store.sqlite import Store


class CheckpointTest(unittest.TestCase):
    def test_checkpoint_with_reader_in_another_process(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            with Store(path) as store:
                program = (
                    'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); '
                    'db.execute("BEGIN"); db.execute("SELECT * FROM documents").fetchall(); '
                    'print("ready",flush=True); sys.stdin.readline(); db.rollback(); db.close()'
                )
                child = subprocess.Popen(
                    [sys.executable, '-u', '-c', program, str(path)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    assert child.stdout is not None
                    self.assertEqual(child.stdout.readline().strip(), 'ready')
                    with store.db:
                        store.db.execute("INSERT INTO documents VALUES ('cross-process','digest')")
                    self.assertEqual(checkpoint(path)['status'], 'partial')
                finally:
                    child.communicate('\n', timeout=10)
                self.assertEqual(child.returncode, 0)
                self.assertEqual(checkpoint(path, 'truncate')['status'], 'complete')

    def test_missing_and_nonwal(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            with self.assertRaises(StoreError):
                checkpoint(path)
            self.assertFalse(path.exists())
            sqlite3.connect(path).close()
            self.assertEqual(checkpoint(path)['status'], 'not_applicable')

    def test_reader_prevents_completion_then_releases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            with Store(path) as writer, Store.open_readonly(path) as reader:
                writer.db.execute('PRAGMA wal_autocheckpoint = 0')
                with reader.read_snapshot():
                    reader.db.execute('SELECT * FROM documents').fetchall()
                    with writer.db:
                        writer.db.execute("INSERT INTO documents VALUES ('x','y')")
                    result = checkpoint(path)
                    self.assertEqual(result['status'], 'partial')
                    self.assertGreater(int(result['remaining_frames']), 0)
                self.assertEqual(checkpoint(path, 'truncate')['status'], 'complete')
                self.assertEqual(writer.db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
