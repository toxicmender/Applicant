"""SQL constraints, historical upgrades and profile revision conflicts."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from applicant.errors import StoreError
from applicant.infra.store.profiles import ProfileRepository, RevisionConflict
from applicant.infra.store.ranking_schema import IMMUTABLE
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.services.preferences import save_profile
from tests.test_ranking_contracts import profile


class RankingStoreTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'applicant.db'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def seed(self):
        db = self.store.db
        db.execute("INSERT INTO candidate_profiles VALUES ('p', 1, '{}')")
        db.execute("INSERT INTO fx_snapshots VALUES ('f', '{}')")
        db.execute("INSERT INTO scoring_configs VALUES ('c', 'hash', '{}')")
        db.execute("INSERT INTO job_versions VALUES ('v', 'j', 'content', 'rank', '{}')")
        db.execute("INSERT INTO job_versions VALUES ('other', 'otherjob', 'content', 'rank', '{}')")
        db.execute("INSERT INTO ranking_runs VALUES ('r', 'p', 1, 'f', 'c', '{}')")
        db.execute("INSERT INTO run_inputs VALUES ('r', 0, 'j', 'v')")
        db.execute("INSERT INTO job_rankings VALUES ('r', 'j', 'v', 50, 0.5, '{}')")

    def test_history_cannot_be_updated_deleted_or_extended_after_completion(self):
        with self.store.db:
            self.seed()
            self.store.db.execute("INSERT INTO run_completions VALUES ('r')")
        for table in IMMUTABLE:
            for operation in (f'UPDATE {table} SET rowid = rowid', f'DELETE FROM {table}'):
                with self.subTest(operation=operation), self.assertRaises(sqlite3.IntegrityError):
                    self.store.db.execute(operation)
                self.store.db.rollback()
        for sql in (
            "INSERT INTO run_inputs VALUES ('r', 1, 'otherjob', 'other')",
            "INSERT INTO job_rankings VALUES ('r', 'otherjob', 'other', NULL, NULL, '{}')",
        ):
            with self.assertRaisesRegex(sqlite3.IntegrityError, 'completed'):
                self.store.db.execute(sql)
            self.store.db.rollback()

    def test_foreign_keys_scores_and_json_are_enforced(self):
        with self.store.db:
            self.seed()
            self.store.db.execute("INSERT INTO run_inputs VALUES ('r', 1, 'otherjob', 'other')")
        for sql in (
            "INSERT INTO run_inputs VALUES ('r', 1, 'otherjob', 'v')",
            "INSERT INTO job_rankings VALUES ('r', 'otherjob', 'other', 101, 0.5, '{}')",
            "INSERT INTO job_rankings VALUES ('r', 'otherjob', 'other', 50, 1.5, '{}')",
            "INSERT INTO fx_snapshots VALUES ('bad', 'not-json')",
            "INSERT INTO job_versions VALUES ('duplicate', 'j', 'content', 'rank', '{}')",
            "INSERT INTO ranking_runs VALUES ('bad', 'missing', 1, 'f', 'c', '{}')",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(sql)
            self.store.db.rollback()

    def test_replace_cannot_bypass_immutable_delete_guards(self):
        with self.store.db:
            self.seed()
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
            self.store.db.execute("INSERT OR REPLACE INTO fx_snapshots VALUES ('f', '{}')")
        self.store.db.rollback()

    def test_profiles_roundtrip_and_reject_stale_edits(self):
        first = save_profile(self.store, profile(), expected_revision=0)
        second = save_profile(
            self.store, profile(profile_revision=2, minimum_base_salary=100), expected_revision=1
        )
        with Store(self.path) as other:
            with self.assertRaises(RevisionConflict):
                save_profile(other, profile(profile_revision=2), expected_revision=1)
            repository = ProfileRepository(other.db)
            self.assertEqual(repository.get('candidate'), second)
            self.assertEqual(repository.get('candidate', 1), first)
        first.weights['compensation'] = Decimal(0)
        self.assertNotEqual(
            ProfileRepository(self.store.db).get('candidate', 1).weights, first.weights
        )

    def test_profiles_reject_invalid_mutated_models_and_nested_transactions(self):
        forged = profile().model_copy(update={'experience_years': Decimal(-1)})
        with self.assertRaises(ValueError):
            save_profile(self.store, forged, expected_revision=0)
        self.store.db.execute('BEGIN')
        with self.assertRaisesRegex(StoreError, 'active transaction'):
            save_profile(self.store, profile(), expected_revision=0)
        self.store.db.rollback()

    def test_upgrade_from_each_existing_version_preserves_legacy_rows(self):
        for version in (1, 2):
            path = self.path.with_name(f'old-{version}.db')
            with (
                mock.patch('applicant.infra.store.sqlite.MIGRATIONS', MIGRATIONS[:version]),
                Store(path) as old,
                old.db,
            ):
                old.db.execute("INSERT INTO documents VALUES ('listing', 'digest')")
                old.db.execute("INSERT INTO jobs VALUES ('listing', 'key', 0, 'fingerprint', '{}')")
                old.db.execute(
                    "INSERT INTO applications VALUES ('log', 0, 'board', 'id', 'fingerprint', 'applied', '{}')"
                )
                old.db.execute("INSERT INTO ratings VALUES ('company', 'source', 'date', '{}')")
                if version == 2:
                    old.db.execute("INSERT INTO financial_documents VALUES ('financials', '{}')")
            with Store(path) as upgraded:
                self.assertEqual(
                    upgraded.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS)
                )
                for table in ('documents', 'jobs', 'applications', 'ratings'):
                    self.assertEqual(
                        upgraded.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 1
                    )
                if version == 2:
                    self.assertEqual(
                        upgraded.db.execute('SELECT count(*) FROM financial_documents').fetchone()[
                            0
                        ],
                        1,
                    )
