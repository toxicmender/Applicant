import unittest
from decimal import Inexact, localcontext
from itertools import permutations
from typing import Any

from applicant.domain.ranking_v2 import JobRankingV2, ScoringConfigurationV2
from applicant.domain.ranking_views import ranked_views
from tests.ranking_fixtures import unknown_result
from tests.test_scoring import decision


def scored(key, score=None, *, state='pass', confidence='1'):
    data: dict[str, Any] = unknown_result(key, key).model_dump() | {'decision': decision(state)}
    if state == 'fail':
        return JobRankingV2.model_validate(
            data
            | {
                'confidence': None,
                'coverage': None,
                'dimensions': tuple(d | {'status': 'not_evaluated'} for d in data['dimensions']),
            }
        )
    if score is not None:
        data['dimensions'][0].update(status='known', score=score, quality='1')
        data.update(overall_score=score, confidence=confidence, coverage='1')
    return JobRankingV2.model_validate(data)


class RankedViewsTest(unittest.TestCase):
    def test_partition_order_ties_unknown_and_permutation_invariance(self):
        results = (
            scored('z', '50'),
            scored('a', '50', confidence='.1'),
            scored('b'),
            scored('review', '100', state='unknown'),
            scored('excluded', state='fail'),
        )
        config = ScoringConfigurationV2()
        for inputs in permutations(results):
            result = ranked_views(inputs, config=config)
            self.assertEqual(tuple(r.job_id for r in result.eligible), ('a', 'z', 'b'))
            self.assertEqual(tuple(r.job_id for r in result.review_required), ('review',))
            self.assertEqual(tuple(r.job_id for r in result.excluded), ('excluded',))
        with self.assertRaises(ValueError):
            ranked_views((results[0], results[0]), config=config)

    def test_display_rounding_and_caller_context(self):
        results = (scored('z', '12.345'), scored('a', '12.344'))
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            result = ranked_views(results, config=ScoringConfigurationV2())
        self.assertEqual(tuple(r.job_id for r in result.eligible), ('a', 'z'))
