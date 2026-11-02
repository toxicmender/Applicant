"""Golden arithmetic, boundary behavior and evidence-source policies for ranking."""

import json
import unittest
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st

from applicant.domain.dimension_scoring import score_dimensions
from applicant.domain.preferences import DIMENSIONS
from applicant.domain.ranking_facts import CategoryRating, RankingFacts, TechnologyRequirements
from applicant.domain.ranking_v2 import DimensionResult, EligibilityDecision, ScoringConfigurationV2
from applicant.domain.scoring import aggregate_dimensions, score_job
from tests.ranking_fixtures import FIXTURES, NOW, compensation_fixture
from tests.test_eligibility import candidate
from tests.test_normalization import normalize, salary
from tests.test_work_routes import route


def source(**changes):
    return compensation_fixture()['source'] | changes


def rating(**changes):
    return CategoryRating.model_validate(
        {
            'source': source(source='glassdoor', quality='structured'),
            'company_id': 'acme',
            'company_url': 'https://example.com/acme',
            'category': 'culture_and_values',
            'value': '4',
            'review_count': 20,
        }
        | changes
    )


def technology(values, **changes):
    return TechnologyRequirements.model_validate(
        {'source': source(), 'technologies': values, 'complete': True} | changes
    )


def decision(status='pass'):
    return EligibilityDecision.model_validate(
        {
            'criteria': [
                {
                    'criterion': 'fixture',
                    'status': status,
                    'reason_code': 'fixture',
                    'explanation': 'Fixture decision.',
                }
            ]
        }
    )


def values(**overrides):
    return tuple(
        DimensionResult.model_validate(
            {'dimension': name, 'status': 'unknown'} | overrides.get(name, {})
        )
        for name in DIMENSIONS
    )


def score(person=None, bundles=(), *, pay=None, config=None, **kwargs):
    return score_dimensions(
        candidate() if person is None else person,
        bundles,
        compensation=pay,
        config=ScoringConfigurationV2() if config is None else config,
        evaluated_at=NOW,
        **kwargs,
    )


class DimensionScoringTest(unittest.TestCase):
    def test_golden_compensation_curve(self):
        fixture = json.loads((FIXTURES / 'scoring.json').read_text(encoding='utf-8'))
        config = ScoringConfigurationV2()
        for case in fixture['salary_curve']:
            person = candidate(
                minimum_base_salary=case['minimum'], preferred_base_salary=case['preferred']
            )
            pay = normalize(
                salary(
                    minimum=case['pay'],
                    maximum=case['pay'],
                    shape='exact',
                    period='annual',
                    currency='EUR',
                ),
                config=config,
            )
            with self.subTest(case=case):
                result = score(person, pay=pay, config=config)[0]
                self.assertEqual(result.score, Decimal(case['score']))

    def test_missing_zero_and_incompatible_compensation(self):
        config = ScoringConfigurationV2()
        person = candidate(preferred_base_salary=100)
        self.assertEqual(score()[0].status, 'not_applicable')
        self.assertEqual(
            score(candidate(minimum_base_salary=0, preferred_base_salary=0))[0].status,
            'not_applicable',
        )
        self.assertEqual(score(person)[0].status, 'unknown')
        for facts in (
            salary(basis=None),
            salary(shape='lower_only', maximum=None),
            salary(basis='net'),
            salary(),
        ):
            pay = normalize(
                facts,
                config=config,
                target='USD' if facts.basis == 'gross' and facts.maximum is not None else 'EUR',
            )
            self.assertEqual(score(person, pay=pay, config=config)[0].status, 'unknown')
        pay = normalize(salary(), config=config)
        with self.assertRaisesRegex(ValueError, 'different scoring configuration'):
            score(person, pay=pay)
        with self.assertRaises(ValueError):
            score_job(
                'j',
                'v',
                person,
                (),
                decision=decision('fail'),
                compensation=pay,
                config=ScoringConfigurationV2(),
                evaluated_at=NOW,
            )

    def test_compensation_evidence_conflicts_tiers_and_numeric_range(self):
        config = ScoringConfigurationV2()
        person = candidate(preferred_base_salary=100000)
        first = salary()
        pay = normalize(first, config=config)

        def evaluate(assertions, **kwargs):
            return score(
                person, (RankingFacts(compensation=assertions),), pay=pay, config=config, **kwargs
            )[0]

        self.assertEqual(evaluate((first,)).status, 'known')
        self.assertEqual(evaluate((first, salary(minimum=7000, maximum=8000))).status, 'unknown')
        self.assertEqual(
            evaluate((salary(minimum=7000, maximum=8000),)).reason_codes,
            ('salary_normalization_mismatch',),
        )
        self.assertEqual(
            evaluate((salary(tier='Senior'), salary(tier='Staff'))).reason_codes,
            ('salary_tier_selection_required',),
        )
        self.assertEqual(evaluate((salary(tier='Senior'),), selected_tier='Senior').status, 'known')
        self.assertEqual(
            evaluate((first,), selected_tier='missing').reason_codes,
            ('salary_evidence_unsupported',),
        )
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_DOWN
            context.traps[Inexact] = True
            self.assertEqual(evaluate((first,)).score, Decimal('52.80'))
        huge = candidate(minimum_base_salary='1e6144', preferred_base_salary='2e6144')
        huge_pay = type(pay).model_validate(
            pay.model_dump()
            | {
                'annual_minimum': '1.5e6144',
                'annual_maximum': '1.5e6144',
                'representative_value': '1.5e6144',
            }
        )
        self.assertEqual(score(huge, pay=huge_pay, config=config)[0].status, 'unknown')

    def test_remote_known_zero_unknown_conflicts_and_best_support(self):
        person = candidate(preferred_work_modes=('Remote',))
        self.assertEqual(score()[1].status, 'not_applicable')
        self.assertEqual(score(person)[1].status, 'unknown')
        onsite = RankingFacts(routes=(route(mode='Onsite', scope='unknown'),), routes_complete=True)
        self.assertEqual(score(person, (onsite,))[1].score, 0)
        self.assertEqual(score(person, (RankingFacts(routes=onsite.routes),))[1].status, 'unknown')
        one = RankingFacts(routes=(route(),), routes_complete=True)
        two = RankingFacts(
            routes=(route(mode='Onsite', scope='unknown', source_id='other'),), routes_complete=True
        )
        self.assertEqual(score(person, (one, two))[1].status, 'unknown')
        inferred = RankingFacts(
            routes=(route('inferred', quality='inferred'),), routes_complete=True
        )
        self.assertEqual(score(person, (inferred, one))[1].quality, 1)
        unsupported = RankingFacts(routes=(route(quality='unknown'),), routes_complete=True)
        self.assertEqual(score(person, (unsupported,))[1].status, 'unknown')

    def test_technical_aliases_completeness_conflicts_and_na(self):
        person = candidate(core_technologies=('Py', 'Java'))

        def evaluate(*requirements):
            return score(person, (RankingFacts(technologies=requirements),))[2]

        self.assertEqual(score()[2].reason_codes, ('candidate_skills_unknown',))
        self.assertEqual(evaluate().status, 'unknown')
        self.assertEqual(evaluate(technology(('python',), complete=False)).status, 'unknown')
        known = evaluate(technology(('Python', 'javascript', 'PYTHON')))
        self.assertEqual(known.score, 50)
        self.assertEqual(known.calculation_terms['total_required'], 2)
        self.assertEqual(evaluate(technology(())).status, 'not_applicable')
        self.assertEqual(evaluate(technology(('python',)), technology(('java',))).status, 'unknown')
        self.assertEqual(
            evaluate(technology(('python',)), technology(('java',), complete=False)).status,
            'unknown',
        )

    def test_review_categories_priority_freshness_counts_and_association(self):
        def evaluate(*ratings, config=None):
            return score(bundles=(RankingFacts(ratings=ratings),), config=config)[3]

        primary = rating()
        secondary = rating(
            source=source(source='ambitionbox'), category='company_culture', value='5'
        )
        self.assertEqual(evaluate(primary, secondary).score, 75)
        newer = rating(source=source(source='glassdoor', observed_at=NOW), value='2')
        self.assertEqual(evaluate(newer, primary).score, 25)
        self.assertEqual(evaluate(rating(review_count=5)).quality, Decimal('.2'))
        self.assertEqual(evaluate(rating(review_count=None)).quality, Decimal('.4'))
        stale = rating(source=source(source='glassdoor', observed_at=NOW - timedelta(days=181)))
        self.assertEqual(evaluate(stale).status, 'unknown')
        boundary = rating(source=source(source='glassdoor', observed_at=NOW - timedelta(days=180)))
        self.assertEqual(evaluate(boundary).status, 'known')
        beyond = rating(
            source=source(source='glassdoor', observed_at=NOW - timedelta(days=180, microseconds=1))
        )
        self.assertEqual(evaluate(beyond).status, 'unknown')
        self.assertEqual(evaluate(rating(source=source(source='unconfigured'))).status, 'unknown')
        self.assertEqual(
            evaluate(rating(source=source(source='glassdoor', quality='unknown'))).status, 'unknown'
        )
        self.assertEqual(
            evaluate(primary, rating(company_id='wrong')).reason_codes,
            ('company_association_ambiguous',),
        )
        self.assertEqual(
            evaluate(
                rating(review_count=None),
                config=ScoringConfigurationV2(missing_review_count_factor=Decimal(0)),
            ).status,
            'unknown',
        )
        stability = rating(source=source(source='ambitionbox'), category='job_security')
        self.assertEqual(
            score(bundles=(RankingFacts(ratings=(stability,)),))[5].reason_codes,
            ('employee_reported_security_proxy',),
        )

    def test_future_consumed_evidence_rejected_and_disabled_dimensions_skip_it(self):
        future = rating(source=source(source='glassdoor', observed_at=NOW + timedelta(days=1)))
        bundle = RankingFacts(ratings=(future,))
        with self.assertRaises(ValueError):
            score(bundles=(bundle,))
        person = candidate(
            weights={name: '1' if name == 'compensation' else '0' for name in DIMENSIONS}
        )
        self.assertEqual(score(person, (bundle,))[3].status, 'disabled')


class ScoringAggregationTest(unittest.TestCase):
    def test_golden_aggregation_and_independent_confidence(self):
        fixture = json.loads((FIXTURES / 'scoring.json').read_text(encoding='utf-8'))['aggregation']
        person = candidate(weights=fixture['weights'])
        dimensions = tuple(DimensionResult.model_validate(d) for d in fixture['dimensions'])
        config = ScoringConfigurationV2()
        result = aggregate_dimensions(person, dimensions, config=config)
        for name, value in fixture['expected'].items():
            self.assertEqual(getattr(result, name), Decimal(value))
        weaker = tuple(
            DimensionResult.model_validate(d.model_dump() | {'quality': '.1'})
            if d.status == 'known'
            else d
            for d in dimensions
        )
        changed = aggregate_dimensions(person, weaker, config=config)
        self.assertEqual(changed.overall_score, result.overall_score)
        self.assertLess(changed.confidence, result.confidence)

    def test_all_unknown_all_na_zero_weights_and_invalid_membership(self):
        config = ScoringConfigurationV2()
        person = candidate()
        self.assertIsNone(aggregate_dimensions(person, values(), config=config).overall_score)
        all_na = values(**{d: {'status': 'not_applicable'} for d in DIMENSIONS})
        self.assertEqual(
            aggregate_dimensions(person, all_na, config=config).notes, ('no_applicable_dimensions',)
        )
        person = candidate(weights={d: '1' if d == 'technical_fit' else '0' for d in DIMENSIONS})
        summary = aggregate_dimensions(
            person,
            values(technical_fit={'status': 'known', 'score': 0, 'quality': 1}),
            config=config,
        )
        self.assertEqual(summary.overall_score, 0)
        self.assertEqual(summary.confidence, 1)
        self.assertEqual(summary.dimensions[0].status, 'disabled')
        for dimensions in (
            values()[:-1],
            (values()[0],) * 6,
            values(technical_fit={'status': 'disabled'}),
        ):
            with self.assertRaises(ValueError):
                aggregate_dimensions(person, dimensions, config=config)

    def test_job_review_and_excluded_results(self):
        config = ScoringConfigurationV2()
        for state in ('pass', 'unknown', 'fail'):
            result = score_job(
                'job',
                'version',
                candidate(),
                (),
                decision=decision(state),
                compensation=None,
                config=config,
                evaluated_at=NOW,
            )
            self.assertIsNone(result.overall_score)
            self.assertEqual(result.confidence, None if state == 'fail' else 0)
            if state == 'fail':
                self.assertTrue(all(d.status == 'not_evaluated' for d in result.dimensions))
        person = candidate(preferred_base_salary=100000)
        for pay, expected in (
            (normalize(salary(), config=config), Decimal('52.80')),
            (normalize(salary(basis=None), config=config), None),
        ):
            result = score_job(
                'job',
                'version',
                person,
                (),
                decision=decision('pass'),
                compensation=pay,
                config=config,
                evaluated_at=NOW,
            )
            self.assertEqual(result.overall_score, expected)

    def test_numeric_weight_limits(self):
        person = candidate(
            weights={
                name: '1e-10000'
                if name == 'compensation'
                else '1'
                if name == 'technical_fit'
                else '0'
                for name in DIMENSIONS
            }
        )
        with self.assertRaisesRegex(ValueError, 'arithmetic range'):
            aggregate_dimensions(
                person,
                values(compensation={'status': 'known', 'score': 1, 'quality': 1}),
                config=ScoringConfigurationV2(),
            )
        person = candidate(
            weights={
                name: '1e-6176'
                if name == 'compensation'
                else '1'
                if name == 'technical_fit'
                else '0'
                for name in DIMENSIONS
            }
        )
        with self.assertRaises(ValueError):
            aggregate_dimensions(
                person,
                values(compensation={'status': 'known', 'score': '.01', 'quality': 1}),
                config=ScoringConfigurationV2(),
            )

    @settings(max_examples=40)
    @given(st.integers(min_value=0, max_value=100), st.integers(min_value=0, max_value=100))
    def test_increasing_one_known_score_cannot_reduce_overall_fit(self, score_one, increment):
        config = ScoringConfigurationV2()
        before = values(compensation={'status': 'known', 'score': score_one, 'quality': '.5'})
        after = values(
            compensation={
                'status': 'known',
                'score': min(100, score_one + increment),
                'quality': '.5',
            }
        )
        one = aggregate_dimensions(candidate(), before, config=config)
        two = aggregate_dimensions(candidate(), after, config=config)
        assert one.overall_score is not None and two.overall_score is not None
        self.assertLessEqual(one.overall_score, two.overall_score)
        self.assertEqual(one.confidence, two.confidence)
