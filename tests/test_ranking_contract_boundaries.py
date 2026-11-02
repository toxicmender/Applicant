"""Coverage audit: reject contradictory score, exclusion and run-history payloads."""

import unittest

from pydantic import ValidationError

from applicant.domain.ranking_v2 import (
    CriterionResult,
    DimensionResult,
    EligibilityDecision,
    EvidenceConflict,
    JobRankingV2,
    RankingRunV2,
    ScoringConfigurationV2,
)
from tests.ranking_fixtures import NOW, unknown_result
from tests.test_normalization import normalize, salary


class RankingContractBoundariesTest(unittest.TestCase):
    def test_dimension_and_aggregate_states_cannot_contradict_each_other(self):
        for fields in (
            {'dimension': 'invalid', 'status': 'unknown'},
            {'dimension': 'compensation', 'status': 'known', 'score': 0, 'quality': 0},
            {'dimension': 'compensation', 'status': 'unknown', 'weighted_contribution': 1},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                DimensionResult.model_validate(fields)
        data = unknown_result().model_dump()
        for change in (
            {'dimensions': data['dimensions'][:-1]},
            {'overall_score': 0},
            {'confidence': '.1', 'coverage': '.1'},
            {
                'dimensions': (
                    data['dimensions'][0] | {'status': 'not_evaluated'},
                    *data['dimensions'][1:],
                )
            },
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                JobRankingV2.model_validate(data | change)

    def test_excluded_jobs_have_neither_fit_nor_computed_dimensions(self):
        data = unknown_result().model_dump()
        decision = {
            'criteria': [
                {
                    'criterion': 'employment',
                    'status': 'fail',
                    'reason_code': 'mismatch',
                    'explanation': 'Contract only.',
                }
            ]
        }
        data |= {'decision': decision}
        with self.assertRaises(ValidationError):
            JobRankingV2.model_validate(data)
        data |= {'confidence': None, 'coverage': None}
        with self.assertRaises(ValidationError):
            JobRankingV2.model_validate(data)
        data['dimensions'] = tuple(d | {'status': 'not_evaluated'} for d in data['dimensions'])
        self.assertEqual(JobRankingV2.model_validate(data).eligibility, 'excluded')

    def test_criteria_and_conflict_identities_remain_unambiguous(self):
        criterion = {
            'criterion': 'employment',
            'status': 'unknown',
            'reason_code': 'missing',
            'explanation': 'Unknown.',
        }
        for criteria in ([], [criterion, criterion]):
            with self.assertRaises(ValidationError):
                EligibilityDecision.model_validate({'criteria': criteria})
        assertion = {
            'evidence_id': 'a',
            'source': 'fixture',
            'observed_at': NOW,
            'normalized_value': 'same',
            'excerpt': 'Statement.',
        }
        conflict = {
            'criterion': 'employment',
            'assertions': (assertion, assertion | {'evidence_id': 'b'}),
            'explanation': 'Conflict.',
            'needs_verification': 'Check employer.',
        }
        with self.assertRaises(ValidationError):
            EvidenceConflict.model_validate(conflict)
        conflict['assertions'] = (
            assertion,
            assertion | {'evidence_id': 'b', 'normalized_value': 'different'},
        )
        with self.assertRaises(ValidationError):
            CriterionResult.model_validate(
                criterion | {'criterion': 'work_route', 'conflicts': (conflict,)}
            )

    def test_run_rejects_duplicate_jobs_and_normalization_from_other_inputs(self):
        first = unknown_result()
        second = unknown_result(version_id='second')
        base = {
            'profile_id': 'p',
            'profile_revision': 1,
            'fx_snapshot_id': 'fx',
            'scoring_config_id': 'config',
            'build_identity': 'fixture',
            'lockfile_digest': 'e' * 64,
            'calculated_at': NOW,
            'job_version_ids': ('version',),
            'results': (first,),
        }
        with self.assertRaises(ValidationError):
            RankingRunV2.model_validate(
                base | {'job_version_ids': ('version', 'second'), 'results': (first, second)}
            )
        config = ScoringConfigurationV2()
        pay = normalize(salary(), config=config)
        result = first.model_dump() | {'compensation': pay}
        with self.assertRaises(ValidationError):
            RankingRunV2.model_validate(base | {'results': (result,)})
