"""Coverage audit: recovery rejects inconsistent SQL keys and JSON payloads."""

import tempfile
import unittest
from pathlib import Path

from applicant.domain.canonical import canonical_json
from applicant.domain.job import Job
from applicant.domain.ranking import JobVersion
from applicant.domain.ranking_v2 import RankingRunV2, ScoringConfigurationV2
from applicant.errors import StoreError
from applicant.infra.store.ranking_runs import RankingRunRepository
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import capture_jobs, save_config, save_fx
from tests.ranking_fixtures import NOW, unknown_result
from tests.test_eligibility import candidate
from tests.test_ranking_contracts import fx


class RankingRecoveryIntegrityTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / 'applicant.db')
        self.addCleanup(self.store.close)
        person = save_profile(self.store, candidate(), expected_revision=0)
        config = save_config(self.store, ScoringConfigurationV2())
        snapshot = save_fx(self.store, fx())
        self.version = capture_jobs(
            self.store,
            (
                JobVersion.capture(
                    Job(source='fixture', id='job', title='Developer'), parser_version='fixture'
                ),
            ),
        )[0]
        self.base = RankingRunV2(
            profile_id=person.profile_id,
            profile_revision=person.profile_revision,
            scoring_config_id=config.scoring_config_id,
            fx_snapshot_id=snapshot.snapshot_id,
            calculated_at=NOW,
            build_identity='fixture',
            lockfile_digest='f' * 64,
        )

    def insert(self, key, payload, result=None):
        with self.store.db:
            self.store.db.execute(
                'INSERT INTO ranking_runs VALUES (?, ?, ?, ?, ?, ?)',
                (
                    key,
                    self.base.profile_id,
                    self.base.profile_revision,
                    self.base.fx_snapshot_id,
                    self.base.scoring_config_id,
                    canonical_json(payload),
                ),
            )
            if result is not None:
                self.store.db.execute(
                    'INSERT INTO run_inputs VALUES (?, ?, ?, ?)',
                    (key, 0, self.version.job_id, self.version.job_version_id),
                )
                self.store.db.execute(
                    'INSERT INTO job_rankings VALUES (?, ?, ?, ?, ?, ?)',
                    (
                        key,
                        self.version.job_id,
                        self.version.job_version_id,
                        None,
                        0,
                        canonical_json(result),
                    ),
                )
            self.store.db.execute('INSERT INTO run_completions VALUES (?)', (key,))

    def test_nonobject_metadata_and_mismatched_identity_are_rejected(self):
        self.insert('array', [])
        self.insert('object', self.base.model_dump(exclude={'results', 'job_version_ids'}))
        for key, message in (
            ('array', 'invalid ranking run metadata'),
            ('object', 'identity mismatch'),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(StoreError, message):
                RankingRunRepository(self.store.db).get(key)

    def test_result_payload_cannot_claim_another_job(self):
        result = unknown_result('wrong-job', self.version.job_version_id)
        self.insert(
            'result',
            self.base.model_dump(exclude={'results', 'job_version_ids'})
            | {'ranking_run_id': 'result'},
            result,
        )
        with self.assertRaisesRegex(StoreError, 'result integrity mismatch'):
            RankingRunRepository(self.store.db).get('result')
