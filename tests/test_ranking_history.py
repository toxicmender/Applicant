"""Persistence journeys recover exact historical inputs without network access."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest import mock

from applicant.domain.canonical import canonical_json
from applicant.domain.job import Job
from applicant.domain.ranking import JobRanking, JobVersion, RankingRun, ScoringConfiguration
from applicant.errors import StoreError
from applicant.infra.store.job_versions import JobVersionRepository
from applicant.infra.store.profiles import ProfileRepository, RevisionConflict
from applicant.infra.store.ranking_common import write_transaction
from applicant.infra.store.ranking_runs import RankingRunRepository
from applicant.infra.store.snapshots import FXSnapshotRepository, ScoringConfigRepository
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import (
    capture_jobs,
    complete_run,
    load_history,
    save_config,
    save_fx,
)
from tests.test_ranking_contracts import fx, profile


class HistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'applicant.db'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.profile = save_profile(self.store, profile(), expected_revision=0)
        self.fx = save_fx(self.store, fx())
        self.config = save_config(self.store, ScoringConfiguration())
        self.job = Job(source='indeed', id='1', title='Developer', remote=True)
        self.version = capture_jobs(
            self.store, (JobVersion.capture(self.job, parser_version='1'),)
        )[0]

    def run_input(self, **changes):
        data = {
            'profile_id': self.profile.profile_id,
            'profile_revision': 1,
            'fx_snapshot_id': self.fx.snapshot_id,
            'scoring_config_id': self.config.scoring_config_id,
            'algorithm_version': 'test-1',
            'build_identity': 'commit-fixture',
            'lockfile_digest': 'a' * 64,
            'job_version_ids': (self.version.job_version_id,),
            'results': (
                JobRanking(
                    job_id=self.version.job_id,
                    job_version_id=self.version.job_version_id,
                    eligibility='review_required',
                ),
            ),
        }
        return RankingRun.model_validate(data | changes)

    def test_reopen_recovers_inputs_after_all_current_inputs_change(self):
        run = complete_run(self.store, self.run_input())
        original = canonical_json(load_history(self.store, run.ranking_run_id))
        save_profile(
            self.store, profile(profile_revision=2, preferred_base_salary=200), expected_revision=1
        )
        save_fx(self.store, fx(snapshot_id='new-fx', rates={'EUR': 1, 'INR': 100}))
        save_config(self.store, ScoringConfiguration(annual_hours=Decimal(1000)))
        self.job.remote = False
        capture_jobs(self.store, (JobVersion.capture(self.job, parser_version='2'),))
        listing = self.path.with_name('listing.json')
        self.store.save_jobs([self.job], listing)
        listing.unlink()
        self.assertEqual(self.store.job_items(listing), [])
        with (
            Store(self.path) as reopened,
            mock.patch('httpx.Client.request', side_effect=AssertionError('network forbidden')),
        ):
            self.assertEqual(canonical_json(load_history(reopened, run.ranking_run_id)), original)

    def test_duplicate_config_and_job_content_reuse_existing_ids(self):
        self.assertEqual(save_config(self.store, ScoringConfiguration()), self.config)
        self.assertEqual(
            capture_jobs(self.store, (JobVersion.capture(self.job, parser_version='1'),))[0],
            self.version,
        )
        self.assertEqual(save_fx(self.store, self.fx), self.fx)
        with self.assertRaisesRegex(StoreError, 'different data'):
            save_fx(self.store, fx(rates={'EUR': 1}))
        self.assertEqual(FXSnapshotRepository(self.store.db).get('fx'), self.fx)

    def test_mismatched_job_result_rolls_back_entire_run(self):
        result = JobRanking(
            job_id='different-job',
            job_version_id=self.version.job_version_id,
            eligibility='eligible',
        )
        run = self.run_input(results=(result,))
        with self.assertRaises(StoreError):
            complete_run(self.store, run)
        for table in ('ranking_runs', 'run_inputs', 'job_rankings', 'run_completions'):
            self.assertEqual(
                self.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0
            )

    def test_runs_preserve_supplied_scores_and_empty_runs(self):
        result = JobRanking(
            job_id=self.version.job_id,
            job_version_id=self.version.job_version_id,
            eligibility='eligible',
            overall_score=Decimal('89.125'),
            confidence=Decimal('.75'),
        )
        run = complete_run(self.store, self.run_input(results=(result,)))
        self.assertEqual(run.results, (result,))
        empty = complete_run(self.store, self.run_input(job_version_ids=(), results=()))
        self.assertEqual(load_history(self.store, empty.ranking_run_id).jobs, ())
        with self.assertRaises(StoreError):
            complete_run(self.store, run)
        self.assertEqual(load_history(self.store, run.ranking_run_id).run, run)

    def test_missing_references_never_fall_back_to_current_inputs(self):
        for repository, key in (
            (FXSnapshotRepository(self.store.db), 'missing'),
            (ScoringConfigRepository(self.store.db), 'missing'),
            (JobVersionRepository(self.store.db), 'missing'),
            (RankingRunRepository(self.store.db), 'missing'),
            (ProfileRepository(self.store.db), 'missing'),
        ):
            with self.subTest(repository=type(repository).__name__), self.assertRaises(StoreError):
                repository.get(key)
        with self.assertRaises(StoreError):
            complete_run(self.store, self.run_input(fx_snapshot_id='missing'))

    def test_selected_jobs_preserve_order_including_unscored_postings(self):
        other = capture_jobs(
            self.store,
            (JobVersion.capture(Job(source='naukri', title='Unknown'), parser_version='1'),),
        )[0]
        run = complete_run(
            self.store,
            self.run_input(job_version_ids=(other.job_version_id, self.version.job_version_id)),
        )
        self.assertEqual(
            [job.job_version_id for job in load_history(self.store, run.ranking_run_id).jobs],
            list(run.job_version_ids),
        )
        with self.assertRaises(ValueError):
            self.run_input(job_version_ids=(self.version.job_version_id,) * 2)
        with self.assertRaises(ValueError):
            self.run_input(job_version_ids=())

    def test_failed_batch_does_not_leave_partial_job_versions(self):
        new = JobVersion.capture(Job(source='naukri', id='new', title='New'), parser_version='1')
        # Existing ID with a different content hash must fail after the first insert.
        collision = JobVersion.capture(
            Job(source='other', id='new', title='Other'), parser_version='1'
        ).model_copy(update={'job_version_id': self.version.job_version_id})
        with self.assertRaises(StoreError):
            capture_jobs(self.store, (new, collision))
        with self.assertRaises(StoreError):
            JobVersionRepository(self.store.db).get(new.job_version_id)

    def test_two_profile_editors_cannot_both_save_the_same_revision(self):
        barrier = threading.Barrier(2)

        def edit():
            with Store(self.path) as store:
                barrier.wait(timeout=5)
                try:
                    save_profile(store, profile(profile_revision=2), expected_revision=1)
                except RevisionConflict:
                    return 'conflict'
                return 'saved'

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(edit) for _ in range(2)]
            self.assertEqual(sorted(f.result(timeout=10) for f in futures), ['conflict', 'saved'])

    def test_two_identical_snapshot_writers_deduplicate(self):
        barrier = threading.Barrier(2)

        def insert():
            with Store(self.path) as store:
                barrier.wait(timeout=5)
                return save_config(store, ScoringConfiguration()).scoring_config_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(insert) for _ in range(2)]
            self.assertEqual(
                [f.result(timeout=10) for f in futures], [self.config.scoring_config_id] * 2
            )

    def test_invalid_historical_payload_is_reported_as_store_error(self):
        with write_transaction(self.store.db):
            self.store.db.execute("INSERT INTO fx_snapshots VALUES ('corrupt', '{}')")
        with self.assertRaisesRegex(StoreError, 'historical FXSnapshot'):
            FXSnapshotRepository(self.store.db).get('corrupt')

    def test_repositories_require_explicit_transaction(self):
        with self.assertRaisesRegex(StoreError, 'caller-owned'):
            FXSnapshotRepository(self.store.db).put(self.fx)
