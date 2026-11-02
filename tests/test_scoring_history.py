"""Computed scores and captured provider inputs survive database recovery unchanged."""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from applicant.domain.eligibility_engine import evaluate_eligibility
from applicant.domain.job import Job
from applicant.domain.normalization import normalize_compensation
from applicant.domain.ranking import Evidence, JobVersion
from applicant.domain.ranking_facts import RankingFacts
from applicant.domain.ranking_v2 import RankingRunV2, ScoringConfigurationV2
from applicant.domain.scoring import score_job
from applicant.infra.store.sqlite import Store
from applicant.reviews.models import CompanyRating
from applicant.services.preferences import save_profile
from applicant.services.ranking_evidence import capture_rating
from applicant.services.ranking_history import (
    capture_jobs,
    complete_run,
    load_history,
    save_config,
    save_fx,
)
from tests.ranking_fixtures import NOW, compensation_facts
from tests.test_eligibility import candidate
from tests.test_ranking_contracts import fx
from tests.test_work_routes import route


class ScoringHistoryTest(unittest.TestCase):
    def test_recovered_inputs_reproduce_computed_score_and_quality(self):
        company = CompanyRating(
            source='glassdoor',
            company='Acme',
            company_id='acme',
            url='https://example.com',
            review_count=20,
            rating_breakdown={'culture_and_values': 4},
        )
        rating_evidence = capture_rating(company, verified_company_id='acme', observed_at=NOW)
        posting = RankingFacts(
            compensation=(compensation_facts(),), routes=(route(),), routes_complete=True
        )
        evidence = (
            Evidence(
                source='posting',
                observed_at=NOW,
                extractor_version='fixture',
                payload=posting.model_dump(mode='json'),
            ),
            *rating_evidence,
        )
        person = candidate(preferred_base_salary=100000, preferred_work_modes=('Remote',))
        bundles = (posting, RankingFacts.model_validate(rating_evidence[0].payload))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'applicant.db'
            with Store(path) as store:
                saved_profile = save_profile(store, person, expected_revision=0)
                config = ScoringConfigurationV2()
                saved_config = save_config(store, config)
                snapshot = save_fx(store, fx(rates={'EUR': 1, 'USD': '1.25'}))
                pay = normalize_compensation(
                    posting.compensation[0],
                    target_currency='EUR',
                    target_basis='gross',
                    fx=snapshot,
                    config=config,
                )
                version = capture_jobs(
                    store,
                    (
                        JobVersion.capture(
                            Job(source='fixture', id='job', title='Developer'),
                            parser_version='fixture',
                            evidence=evidence,
                        ),
                    ),
                )[0]
                evaluated = evaluate_eligibility(
                    person, bundles, compensation=pay, config=config, evaluated_at=NOW
                )
                result = score_job(
                    version.job_id,
                    version.job_version_id,
                    person,
                    bundles,
                    decision=evaluated.decision,
                    compensation=pay,
                    config=config,
                    evaluated_at=NOW,
                )
                self.assertEqual(result.overall_score, Decimal('74.08'))
                self.assertEqual(result.confidence, Decimal('.5450'))
                run = complete_run(
                    store,
                    RankingRunV2(
                        profile_id=saved_profile.profile_id,
                        profile_revision=saved_profile.profile_revision,
                        scoring_config_id=saved_config.scoring_config_id,
                        fx_snapshot_id=snapshot.snapshot_id,
                        build_identity='fixture',
                        lockfile_digest='d' * 64,
                        calculated_at=NOW,
                        job_version_ids=(version.job_version_id,),
                        results=(result,),
                    ),
                )
            company.rating_breakdown['culture_and_values'] = 1
            with (
                Store(path) as reopened,
                mock.patch('httpx.Client.request', side_effect=AssertionError('network')),
            ):
                history = load_history(reopened, run.ranking_run_id)
                assert isinstance(history.run, RankingRunV2)
                assert isinstance(history.config, ScoringConfigurationV2)
                recovered = tuple(
                    RankingFacts.model_validate(e.payload)
                    for e in history.jobs[0].evidence
                    if e.payload.get('kind') == 'ranking_facts'
                )
                reranked = score_job(
                    version.job_id,
                    version.job_version_id,
                    history.profile,
                    recovered,
                    decision=history.run.results[0].decision,
                    compensation=history.run.results[0].compensation,
                    config=history.config,
                    evaluated_at=history.run.calculated_at,
                )
                self.assertEqual(reranked, history.run.results[0])
