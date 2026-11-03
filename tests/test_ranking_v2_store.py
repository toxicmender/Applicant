"""Schema 3 stores both contract generations without modifying historical rows."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from applicant.domain.canonical import canonical_json
from applicant.domain.job import Job
from applicant.domain.ranking import JobVersion, ScoringConfiguration
from applicant.domain.ranking_v2 import (
    FXSnapshotV2,
    RankingRunV2,
    ScoringConfigurationV2,
    upgrade_profile,
)
from applicant.errors import StoreError
from applicant.infra.store.profiles import ProfileRepository
from applicant.infra.store.snapshots import FXSnapshotRepository
from applicant.infra.store.sqlite import MIGRATIONS, Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import (
    capture_jobs,
    complete_run,
    load_history,
    save_config,
    save_fx,
)
from tests.ranking_fixtures import NOW, unknown_result
from tests.test_ranking_contracts import fx, profile


class EngineHistoryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'applicant.db'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def test_v2_run_recovers_after_profile_and_current_inputs_change(self):
        legacy = save_profile(self.store, profile(), expected_revision=0)
        original = canonical_json(legacy)
        upgraded = save_profile(
            self.store, upgrade_profile(profile(), updated_at=NOW), expected_revision=1
        )
        config = save_config(self.store, ScoringConfigurationV2())
        snapshot = save_fx(
            self.store, FXSnapshotV2.model_validate(fx().model_dump() | {'schema_version': 2})
        )
        version = capture_jobs(
            self.store,
            (
                JobVersion.capture(
                    Job(source='indeed', id='1', title='Developer'), parser_version='fixture'
                ),
            ),
        )[0]
        run = RankingRunV2(
            profile_id=upgraded.profile_id,
            profile_revision=2,
            fx_snapshot_id=snapshot.snapshot_id,
            scoring_config_id=config.scoring_config_id,
            build_identity='fixture',
            lockfile_digest='b' * 64,
            calculated_at=NOW,
            job_version_ids=(version.job_version_id,),
            results=(unknown_result(version.job_id, version.job_version_id),),
        )
        self.assertEqual(complete_run(self.store, run), run)
        expected = canonical_json(load_history(self.store, run.ranking_run_id))
        save_fx(self.store, fx(snapshot_id='newer'))
        save_config(self.store, ScoringConfigurationV2(fx_max_age_days=3))
        with (
            Store(self.path) as reopened,
            mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
        ):
            history = load_history(reopened, run.ranking_run_id)
            self.assertIsInstance(history.run, RankingRunV2)
            self.assertEqual(canonical_json(history), expected)
            self.assertEqual(
                canonical_json(ProfileRepository(reopened.db).get(legacy.profile_id, 1)), original
            )
            self.assertEqual(
                reopened.db.execute('PRAGMA user_version').fetchone()[0], len(MIGRATIONS)
            )

    def test_engine_run_rejects_legacy_configuration_atomically(self):
        save_profile(self.store, profile(), expected_revision=0)
        snapshot = save_fx(self.store, fx())
        config = save_config(self.store, ScoringConfiguration())
        run = RankingRunV2(
            profile_id='candidate',
            profile_revision=1,
            fx_snapshot_id=snapshot.snapshot_id,
            scoring_config_id=config.scoring_config_id,
            build_identity='fixture',
            lockfile_digest='b' * 64,
            calculated_at=NOW,
        )
        with self.assertRaisesRegex(StoreError, 'matching engine configuration'):
            complete_run(self.store, run)
        self.assertEqual(
            self.store.db.execute('SELECT count(*) FROM ranking_runs').fetchone()[0], 0
        )

    def test_unknown_version_is_rejected_instead_of_using_defaults(self):
        data = fx().model_dump() | {'schema_version': 999}
        with self.store.db:
            self.store.db.execute(
                'INSERT INTO fx_snapshots VALUES (?, ?)', ('fx', canonical_json(data))
            )
        with self.assertRaisesRegex(StoreError, 'historical FXSnapshot'):
            FXSnapshotRepository(self.store.db).get('fx')

    def test_cache_selection_uses_effective_age_original_retrieval_and_stable_id(self):
        for key, age, retrieved, provenance in (
            ('stale', 8, 0, 'cache'),
            ('static', 0, 0, 'static'),
            ('b', 1, 1, 'cache'),
            ('a', 1, 1, 'cache'),
            ('older-retrieval', 1, 2, 'cache'),
        ):
            save_fx(
                self.store,
                fx(
                    snapshot_id=key,
                    effective_date=(NOW - timedelta(days=age)).date(),
                    retrieved_at=NOW - timedelta(hours=retrieved),
                    captured_at=NOW,
                    provenance=provenance,
                    dataset_version='fixture',
                ),
            )
        repository = FXSnapshotRepository(self.store.db)

        def select(
            *, evaluated_at=NOW, max_age_days=7, target_currency='EUR', source_currencies=None
        ):
            return repository.latest_eligible(
                evaluated_at=evaluated_at,
                max_age_days=max_age_days,
                target_currency=target_currency,
                source_currencies={'INR'} if source_currencies is None else source_currencies,
            )

        selected = select()
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.snapshot_id, 'a')
        self.assertIsNone(select(max_age_days=0))
        self.assertIsNone(select(target_currency='USD'))
        self.assertIsNone(select(source_currencies={'USD'}))
        self.assertIsNone(select(evaluated_at=NOW - timedelta(days=2)))
        with self.assertRaises(ValueError):
            select(max_age_days=-1)
