"""Complete pure evaluation and orchestration journeys, including exact replay."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import httpx

from applicant.domain.canonical import canonical_json
from applicant.domain.job import Job
from applicant.domain.ranking import Evidence, JobVersion, RankingRun, ScoringConfiguration
from applicant.domain.ranking_engine import evaluate_jobs
from applicant.domain.ranking_facts import RankingFacts
from applicant.domain.ranking_salary import SalaryParse
from applicant.domain.ranking_v2 import RankingRunV3, ScoringConfigurationV2
from applicant.errors import StoreError
from applicant.infra.fx import FrankfurterFX
from applicant.infra.store.sqlite import Store
from applicant.services.preferences import save_profile
from applicant.services.ranking import prepare_job, rank_jobs
from applicant.services.ranking_history import (
    capture_jobs,
    complete_run,
    load_history,
    save_config,
    save_fx,
)
from applicant.services.ranking_replay import (
    ReplayEnvironmentMismatch,
    UnsupportedAlgorithm,
    replay_run,
)
from tests.ranking_fixtures import FIXTURES, NOW, compensation_facts
from tests.test_eligibility import candidate
from tests.test_ranking_contracts import fx
from tests.test_work_routes import route


def version(key='job', *, bundles=(), salary_text=None, remote=None, employment_type=None):
    value = JobVersion.capture(
        Job(
            source='fixture',
            id=key,
            title='Developer',
            salary=salary_text,
            remote=remote,
            employment_type=employment_type,
        ),
        parser_version='fixture',
        evidence=tuple(
            Evidence(
                source='fixture',
                observed_at=NOW,
                extractor_version='fixture',
                payload=b.model_dump(mode='json'),
            )
            for b in bundles
        ),
    )
    return JobVersion.model_validate(value.model_dump() | {'captured_at': NOW})


class RankingEngineTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'applicant.db'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.person = candidate(preferred_base_salary=100000)
        save_profile(self.store, self.person, expected_revision=0)
        self.config = ScoringConfigurationV2()

    def rank(self, jobs=(), **changes):
        return rank_jobs(
            self.store,
            **(
                {
                    'profile_id': self.person.profile_id,
                    'profile_revision': 1,
                    'jobs': jobs,
                    'config': self.config,
                    'evaluated_at': NOW,
                    'build_identity': 'fixture-build',
                    'lockfile_digest': 'a' * 64,
                    'offline': True,
                    'supplied_fx': fx(rates={'EUR': 1, 'USD': '1.25'}),
                }
                | changes
            ),
        )

    def replay(self, run):
        return replay_run(
            self.store, run.ranking_run_id, build_identity='fixture-build', lockfile_digest='a' * 64
        )

    def test_mixed_membership_dedup_and_reopened_history_ignore_current_state(self):
        good = RankingFacts(
            compensation=(compensation_facts(),), routes=(route(),), routes_complete=True
        )
        jobs = (
            version('eligible', bundles=(good,)),
            version('review'),
            version(
                'excluded',
                bundles=(
                    RankingFacts(
                        routes=(route(mode='Onsite', scope='unknown'),), routes_complete=True
                    ),
                ),
            ),
        )
        # Required remote makes the final job excluded; unknown data stays in review.
        self.person = candidate(preferred_base_salary=100000, required_work_modes=('Remote',))
        from applicant.services.preferences import save_profile

        person2 = type(self.person).model_validate(
            self.person.model_dump() | {'profile_revision': 2}
        )
        save_profile(self.store, person2, expected_revision=1)
        first = self.rank(jobs, profile_revision=2)
        second = self.rank(jobs, profile_revision=2, config=ScoringConfigurationV2())
        self.assertEqual(first.job_version_ids, second.job_version_ids)
        self.assertEqual(first.scoring_config_id, second.scoring_config_id)
        self.assertEqual(
            tuple(r.eligibility for r in first.results), ('eligible', 'review_required', 'excluded')
        )
        self.assertEqual(self.replay(first).matches, True)
        expected = canonical_json(load_history(self.store, first.ranking_run_id))
        save_profile(
            self.store,
            type(person2).model_validate(
                person2.model_dump()
                | {
                    'profile_revision': 3,
                    'preferred_base_salary': 200000,
                }
            ),
            expected_revision=2,
        )
        save_fx(self.store, fx(snapshot_id='new-rates', rates={'EUR': 1, 'USD': '2'}))
        save_config(self.store, ScoringConfigurationV2.model_validate({'annual_hours': '1800'}))
        capture_jobs(self.store, (version('eligible', salary_text='Different'),))
        with (
            Store(self.path) as reopened,
            mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
        ):
            changes = reopened.db.total_changes
            checked = replay_run(
                reopened,
                first.ranking_run_id,
                build_identity='fixture-build',
                lockfile_digest='a' * 64,
            )
            self.assertTrue(checked.matches)
            self.assertEqual(reopened.db.total_changes, changes)
            self.assertEqual(canonical_json(load_history(reopened, first.ranking_run_id)), expected)

    def test_tier_choices_are_per_run_and_do_not_change_job_hashes(self):
        bundle = RankingFacts(
            compensation=(
                compensation_facts(tier='Senior'),
                compensation_facts(tier='Staff', minimum=9000, maximum=10000),
            ),
            routes=(route(),),
            routes_complete=True,
        )
        job = version(bundles=(bundle,))
        first = self.rank((job,), selected_tiers={job.job_id: 'Senior'})
        second = self.rank((job,), selected_tiers={job.job_id: 'Staff'})
        self.assertEqual(first.job_version_ids, second.job_version_ids)
        self.assertNotEqual(first.results[0].overall_score, second.results[0].overall_score)
        self.assertTrue(self.replay(first).matches)
        self.assertTrue(self.replay(second).matches)
        unknown = self.rank((job,))
        missing = self.rank((job,), selected_tiers={job.job_id: 'missing'})
        self.assertIsNone(unknown.results[0].compensation)
        self.assertIsNone(missing.results[0].compensation)
        self.assertTrue(self.replay(unknown).matches)

    def test_legacy_fields_are_captured_conservatively_and_preparation_is_idempotent(self):
        job = version(
            salary_text='USD 5,000-6,000 monthly gross base',
            remote=True,
            employment_type='contract',
        )
        prepared = prepare_job(job, evaluated_at=NOW)
        self.assertEqual(prepare_job(prepared, evaluated_at=NOW), prepared)
        run = self.rank((job,))
        self.assertEqual(run.results[0].eligibility, 'review_required')
        self.assertIsNotNone(run.results[0].compensation)
        self.assertTrue(self.replay(run).matches)
        ambiguous = prepare_job(
            version(
                'ambiguous', salary_text='Negotiable', remote=False, employment_type='FULL_TIME'
            ),
            evaluated_at=NOW,
        )
        self.assertEqual(prepare_job(ambiguous, evaluated_at=NOW), ambiguous)
        self.assertTrue(
            any(e.payload.get('kind') == 'ranking_salary_parse' for e in ambiguous.evidence)
        )
        self.assertTrue(self.replay(self.rank((ambiguous,))).matches)

    def test_validation_and_calculation_failure_never_publish_partial_runs(self):
        job = version()
        for changes in (
            {'jobs': (job, job)},
            {'selected_tiers': {'other': 'Senior'}},
            {'selected_tiers': {job.job_id: ' '}},
            {'lockfile_digest': 'bad'},
            {
                'jobs': (
                    JobVersion.model_validate(
                        job.model_dump() | {'captured_at': NOW + timedelta(days=1)}
                    ),
                )
            },
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.rank(**({'jobs': (job,)} | changes))
        with (
            mock.patch(
                'applicant.services.ranking.evaluate_jobs',
                side_effect=RuntimeError('calculation failed'),
            ),
            self.assertRaises(RuntimeError),
        ):
            self.rank((job,))
        with self.assertRaises(StoreError):
            self.store.db.execute('BEGIN IMMEDIATE')
            try:
                self.rank()
            finally:
                self.store.db.rollback()
        self.assertEqual(
            self.store.db.execute('SELECT count(*) FROM ranking_runs').fetchone()[0], 0
        )
        self.assertEqual(
            self.store.db.execute('SELECT count(*) FROM run_completions').fetchone()[0], 0
        )

    def test_empty_run_environment_checks_unsupported_and_mismatch_report(self):
        empty = self.rank()
        self.assertIsInstance(empty, RankingRunV3)
        self.assertTrue(self.replay(empty).matches)
        with self.assertRaises(ReplayEnvironmentMismatch):
            replay_run(
                self.store,
                empty.ranking_run_id,
                build_identity='different-build',
                lockfile_digest='a' * 64,
            )
        config = save_config(self.store, ScoringConfiguration())
        old = complete_run(
            self.store,
            RankingRun(
                profile_id=self.person.profile_id,
                profile_revision=1,
                scoring_config_id=config.scoring_config_id,
                fx_snapshot_id=empty.fx_snapshot_id,
                algorithm_version='old-engine',
                build_identity='fixture-build',
                lockfile_digest='a' * 64,
            ),
        )
        with self.assertRaises(UnsupportedAlgorithm):
            self.replay(old)
        job = version()
        run = self.rank((job,))
        altered = run.results[0].model_copy(update={'job_id': 'changed'})
        with mock.patch('applicant.services.ranking_replay.evaluate_jobs', return_value=(altered,)):
            report = self.replay(run)
        self.assertFalse(report.matches)
        self.assertEqual(report.mismatched_job_version_ids, run.job_version_ids)

    def test_unknown_evidence_and_pure_input_validation(self):
        unknown = Evidence(
            source='future',
            observed_at=NOW,
            extractor_version='2',
            payload={'kind': 'ranking_facts', 'schema_version': 99},
        )
        job = version().model_copy(update={'evidence': (unknown,)})
        run = self.rank((job,))
        self.assertIn(
            'unsupported_evidence_schema', run.results[0].decision.criteria[-1].explanation
        )
        with self.assertRaises(ValueError):
            evaluate_jobs(self.person, (job, job), fx=fx(), config=self.config, evaluated_at=NOW)
        with self.assertRaises(ValueError):
            evaluate_jobs(
                self.person,
                (job,),
                fx=fx(),
                config=self.config,
                evaluated_at=NOW,
                selected_tiers={'other': 'Senior'},
            )

    def test_pure_evidence_boundaries_and_salary_conflicts(self):
        def evaluate(job):
            return evaluate_jobs(
                self.person,
                (job,),
                fx=fx(rates={'EUR': 1, 'USD': '1.25'}),
                config=self.config,
                evaluated_at=NOW,
            )

        first = compensation_facts()
        other = compensation_facts(
            minimum=7000, maximum=8000, source=first.source.model_dump() | {'evidence_id': 'other'}
        )
        conflict = version(bundles=(RankingFacts(compensation=(first, other)),))
        self.assertIn(
            'conflicting_salary_evidence', evaluate(conflict)[0].decision.criteria[-1].explanation
        )
        unsupported = compensation_facts(source=first.source.model_dump() | {'quality': 'unknown'})
        self.assertIsNone(
            evaluate(version(bundles=(RankingFacts(compensation=(unsupported,)),)))[0].compensation
        )
        future = compensation_facts(
            source=first.source.model_dump() | {'observed_at': NOW + timedelta(days=1)}
        )
        with self.assertRaises(ValueError):
            evaluate(version(bundles=(RankingFacts(compensation=(future,)),)))
        with self.assertRaises(ValueError):
            evaluate(version().model_copy(update={'captured_at': NOW + timedelta(days=1)}))
        evidence = Evidence(
            source='fixture',
            observed_at=NOW + timedelta(days=1),
            extractor_version='fixture',
            payload=RankingFacts().model_dump(mode='json'),
        )
        with self.assertRaises(ValueError):
            evaluate(version().model_copy(update={'evidence': (evidence,)}))
        for payload in (
            {'kind': 'ranking_salary_parse', 'schema_version': 99},
            SalaryParse().model_dump(mode='json') | {'kind': 'ranking_salary_parse'},
            {'kind': 'unconsumed-raw-evidence'},
        ):
            evidence = Evidence(
                source='fixture', observed_at=NOW, extractor_version='fixture', payload=payload
            )
            self.assertEqual(
                len(evaluate(version().model_copy(update={'evidence': (evidence,)}))), 1
            )

    def test_completion_failure_rolls_back_run_and_membership(self):
        self.store.db.execute(
            "CREATE TRIGGER fail_result BEFORE INSERT ON job_rankings BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
        with self.assertRaises(StoreError):
            self.rank((version(),))
        for table in ('ranking_runs', 'run_inputs', 'job_rankings', 'run_completions'):
            self.assertEqual(
                self.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0
            )

    def test_one_provider_request_for_batch_precedes_completion_transaction(self):
        calls = []

        def handler(request):
            self.assertFalse(self.store.db.in_transaction)
            calls.append(request)
            return httpx.Response(200, content=(FIXTURES / 'ecb-rates.json').read_bytes())

        jobs = tuple(
            version(str(index), salary_text='USD 5000 monthly gross base') for index in range(3)
        )
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            run = self.rank(
                jobs, offline=False, supplied_fx=None, provider=FrankfurterFX(client=client)
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(run.results), 3)
        self.assertTrue(
            all(
                r.compensation is not None and r.compensation.status == 'available'
                for r in run.results
            )
        )
        self.assertTrue(self.replay(run).matches)
