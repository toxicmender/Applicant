"""Review reasons remain readable and attributable after persistence and reopening."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from applicant.domain.eligibility_engine import evaluate_eligibility
from applicant.domain.job import Job
from applicant.domain.ranking import Evidence, JobVersion
from applicant.domain.ranking_facts import RankingFacts, WorkRoute
from applicant.domain.ranking_v2 import RankingRunV2, ScoringConfigurationV2
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking_history import (
    capture_jobs,
    complete_run,
    load_history,
    save_config,
    save_fx,
)
from tests.ranking_fixtures import FIXTURES, NOW, unknown_result
from tests.test_eligibility import candidate
from tests.test_ranking_contracts import fx


class EligibilityHistoryTest(unittest.TestCase):
    def test_conflict_golden_fixture_survives_offline_history_load(self):
        fixture = json.loads((FIXTURES / 'route-conflict.json').read_text(encoding='utf-8'))
        bundles = tuple(
            RankingFacts(routes=(WorkRoute.model_validate(value),), routes_complete=True)
            for value in fixture['assertions']
        )
        person = candidate(work_country='IN')
        config = ScoringConfigurationV2()
        evaluated = evaluate_eligibility(
            person, bundles, compensation=None, config=config, evaluated_at=NOW
        )
        self.assertEqual(evaluated.decision.status, fixture['expected_status'])
        criterion = evaluated.decision.criteria[-1]
        self.assertEqual(criterion.reason_code, fixture['expected_reason'])
        self.assertIn('US applicants', criterion.explanation)
        self.assertIn('worldwide', criterion.explanation)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'applicant.db'
            with Store(path) as store:
                saved_profile = save_profile(store, person, expected_revision=0)
                saved_config = save_config(store, config)
                snapshot = save_fx(store, fx())
                version = capture_jobs(
                    store,
                    (
                        JobVersion.capture(
                            Job(source='fixture', id='job', title='Developer'),
                            parser_version='fixture-1',
                            evidence=tuple(
                                Evidence(
                                    source='fixture',
                                    observed_at=NOW,
                                    extractor_version='fixture-1',
                                    payload=bundle.model_dump(mode='json'),
                                )
                                for bundle in bundles
                            ),
                        ),
                    ),
                )[0]
                result = unknown_result(version.job_id, version.job_version_id).model_copy(
                    update={'decision': evaluated.decision}
                )
                run = complete_run(
                    store,
                    RankingRunV2(
                        profile_id=saved_profile.profile_id,
                        profile_revision=saved_profile.profile_revision,
                        fx_snapshot_id=snapshot.snapshot_id,
                        scoring_config_id=saved_config.scoring_config_id,
                        build_identity='fixture',
                        lockfile_digest='c' * 64,
                        calculated_at=NOW,
                        job_version_ids=(version.job_version_id,),
                        results=(result,),
                    ),
                )
            with (
                Store(path) as reopened,
                mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
            ):
                loaded = load_history(reopened, run.ranking_run_id)
                assert isinstance(loaded.run, RankingRunV2)
                restored = loaded.run.results[0].decision.criteria[-1]
                self.assertEqual(restored, criterion)
                self.assertEqual(len(restored.conflicts[0].assertions), 2)
                assert isinstance(loaded.config, ScoringConfigurationV2)
                recovered = tuple(
                    RankingFacts.model_validate(e.payload) for e in loaded.jobs[0].evidence
                )
                reevaluated = evaluate_eligibility(
                    loaded.profile,
                    recovered,
                    compensation=None,
                    config=loaded.config,
                    evaluated_at=loaded.run.calculated_at,
                )
                self.assertEqual(reevaluated.decision, evaluated.decision)
