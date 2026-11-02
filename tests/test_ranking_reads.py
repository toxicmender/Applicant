"""Read-only ranking preflight must never synchronize files or publish history."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from applicant.domain.job import Job
from applicant.domain.ranking import RankingRun, ScoringConfiguration
from applicant.errors import ConfigError, StoreError
from applicant.infra.store.listing_snapshots import listing_snapshot
from applicant.infra.store.ranking_runs import RankingRunRepository
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import complete_run, load_history, save_config, save_fx
from tests.test_ranking_contracts import NOW, fx, profile


class RankingReadsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'réad # percent%.db'
        self.listing = self.root / 'nested' / 'listing.json'
        self.job = Job(source='indeed', title='Engineer', id='1')

    def test_missing_database_does_not_create_directories(self):
        path = self.root / 'absent' / 'applicant.db'
        with self.assertRaises(StoreError):
            Store.open_readonly(path)
        self.assertFalse(path.parent.exists())
        self.assertEqual(listing_snapshot(self.listing), ())
        self.assertFalse(self.listing.parent.exists())

    def test_schema_errors_and_corruption_release_connection_without_migration(self):
        for version in (0, len(MIGRATIONS) - 1, len(MIGRATIONS) + 1):
            with self.subTest(version=version):
                with sqlite3.connect(self.path) as db:
                    db.execute(f'PRAGMA user_version = {version}')
                db.close()
                original = self.path.read_bytes()
                with self.assertRaisesRegex(StoreError, 'upgrade|newer'):
                    Store.open_readonly(self.path)
                self.assertEqual(self.path.read_bytes(), original)
                self.path.unlink()
        self.path.write_bytes(b'not sqlite')
        with self.assertRaises(StoreError):
            Store.open_readonly(self.path)
        self.path.unlink()

    def test_reads_do_not_initialize_change_permissions_or_restore_deleted_exports(self):
        with Store(self.path) as writer:
            writer.save_jobs([self.job], self.listing)
        self.listing.unlink()
        original = self.path.read_bytes()
        with (
            mock.patch('applicant.infra.store.sqlite.make_private', side_effect=AssertionError),
            mock.patch.object(Store, '_migrate', side_effect=AssertionError),
            Store.open_readonly(self.path) as reader,
        ):
            with reader.read_snapshot():
                self.assertEqual(listing_snapshot(self.listing, store=reader), (self.job,))
                self.assertEqual(reader.listing_items_snapshot(self.root / 'other.json'), [])
                self.assertEqual(RankingRunRepository(reader.db).completed(), ())
            with self.assertRaises(sqlite3.OperationalError):
                reader.db.execute("INSERT INTO documents VALUES ('x', NULL)")
            self.assertEqual(reader.db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
        self.assertFalse(self.listing.exists())
        self.assertEqual(self.path.read_bytes(), original)

    def test_external_file_precedence_does_not_sync_database(self):
        with Store(self.path) as writer:
            writer.save_jobs([self.job], self.listing)
        self.listing.write_text('{"list": []}', encoding='utf-8')
        with Store.open_readonly(self.path) as reader:
            self.assertEqual(listing_snapshot(self.listing, store=reader), ())
            self.assertEqual(reader.listing_items_snapshot(self.listing)[0]['id'], '1')
            self.listing.write_text('{', encoding='utf-8')
            with self.assertRaises(ConfigError):
                listing_snapshot(self.listing, store=reader)
            self.assertEqual(reader.listing_items_snapshot(self.listing)[0]['id'], '1')

    def test_strict_listing_errors_include_invalid_record_index(self):
        path = self.root / 'input.json'
        for text in (
            '{}',
            '[]',
            '{"list": {}}',
            '{"list": [], "unknown": 1}',
            '{"list": [], "list": []}',
            '{"list": [NaN]}',
            '{"list": [{"unused": 1e999}]}',
            '{"list": [null]}',
            '{"list": [{}]}',
            '{"list": [{"source": " ", "title": "x"}]}',
            '{"list": [{"source": "x", "title": "x", "source": "y"}]}',
        ):
            with self.subTest(text=text):
                path.write_text(text, encoding='utf-8')
                with self.assertRaises(ConfigError):
                    listing_snapshot(path)
                self.assertEqual(path.read_text(encoding='utf-8'), text)
        path.write_text('{"list": [null]}', encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, r'list\[0\]'):
            listing_snapshot(path)
        path.write_bytes(b'\xff')
        with self.assertRaises(ConfigError):
            listing_snapshot(path)
        with (
            mock.patch.object(Path, 'read_text', side_effect=PermissionError('denied')),
            self.assertRaises(StoreError),
        ):
            listing_snapshot(path)

    def test_listing_order_legacy_fields_and_board_scoped_duplicates(self):
        path = self.root / 'input.json'
        other = self.job.model_dump() | {
            'source': 'linkedin',
            'legacy': True,
            'experience_min': 1.5,
        }
        path.write_text(json.dumps({'list': [other, self.job.model_dump()]}), encoding='utf-8')
        self.assertEqual([j.source for j in listing_snapshot(path)], ['linkedin', 'indeed'])
        path.write_text(json.dumps({'list': [other, other]}), encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'duplicate job identity'):
            listing_snapshot(path)

    def test_snapshot_is_consistent_across_a_concurrent_commit_and_released_on_error(self):
        with Store(self.path) as writer, Store.open_readonly(self.path) as reader:
            with reader.read_snapshot():
                self.assertEqual(reader.listing_items_snapshot(self.listing), [])
                writer.save_jobs([self.job], self.listing)
                self.assertEqual(reader.listing_items_snapshot(self.listing), [])
            self.assertEqual(listing_snapshot(self.listing, store=reader), (self.job,))
            with self.assertRaisesRegex(RuntimeError, 'cancel'), reader.read_snapshot():
                raise RuntimeError('cancel')
            self.assertFalse(reader.db.in_transaction)
            with (
                self.assertRaisesRegex(StoreError, 'active transaction'),
                reader.read_snapshot(),
                reader.read_snapshot(),
            ):
                pass
            with self.assertRaises(StoreError), reader.read_snapshot():
                reader.db.execute('SELECT * FROM absent_table')
            self.assertFalse(reader.db.in_transaction)

    def test_corrupt_stored_listing_is_not_empty(self):
        with Store(self.path) as writer:
            writer.save_jobs([self.job], self.listing)
            self.listing.unlink()
            with writer.db:
                writer.db.execute("UPDATE jobs SET payload = 'null'")
            with self.assertRaises(ConfigError):
                listing_snapshot(self.listing, store=writer)
            with writer.db:
                writer.db.execute("UPDATE jobs SET payload = 'invalid json'")
            with self.assertRaises(StoreError):
                listing_snapshot(self.listing, store=writer)

    def test_completed_history_filters_orders_and_recovers_exact_runs(self):
        with Store(self.path) as writer:
            saved = save_profile(writer, profile(), expected_revision=0)
            save_profile(writer, profile(profile_id='other'), expected_revision=0)
            rates = save_fx(writer, fx())
            config = save_config(writer, ScoringConfiguration())
            for run_id, profile_id, at in (
                ('z', saved.profile_id, NOW),
                ('a', saved.profile_id, NOW),
                ('new', 'other', NOW + timedelta(days=1)),
            ):
                complete_run(
                    writer,
                    RankingRun(
                        ranking_run_id=run_id,
                        profile_id=profile_id,
                        profile_revision=1,
                        fx_snapshot_id=rates.snapshot_id,
                        scoring_config_id=config.scoring_config_id,
                        algorithm_version='fixture',
                        build_identity='test',
                        lockfile_digest='a' * 64,
                        calculated_at=at,
                    ),
                )
            # An interrupted producer's metadata must not appear in history.
            with writer.db:
                writer.db.execute(
                    "INSERT INTO ranking_runs SELECT 'partial', profile_id, profile_revision, "
                    "fx_snapshot_id, scoring_config_id, payload FROM ranking_runs WHERE ranking_run_id='a'"
                )
        with Store.open_readonly(self.path) as reader, reader.read_snapshot():
            repo = RankingRunRepository(reader.db)
            self.assertEqual([r.ranking_run_id for r in repo.completed()], ['new', 'a', 'z'])
            self.assertEqual(
                [r.ranking_run_id for r in repo.completed(profile_id='candidate')], ['a', 'z']
            )
            self.assertEqual(repo.completed(profile_id="' OR 1=1 --"), ())
            self.assertEqual(load_history(reader, 'a').profile, saved)

    def test_completed_enumeration_preserves_integrity_checks(self):
        with Store(self.path) as writer, writer.db:
            writer.db.execute('PRAGMA foreign_keys = OFF')
            writer.db.execute("INSERT INTO ranking_runs VALUES ('bad', 'p', 1, 'fx', 'cfg', '{}')")
            writer.db.execute("INSERT INTO run_completions VALUES ('bad')")
        with Store.open_readonly(self.path) as reader, self.assertRaises(StoreError):
            RankingRunRepository(reader.db).completed()
