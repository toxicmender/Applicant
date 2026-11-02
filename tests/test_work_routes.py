"""Route decisions match an exhaustive oracle; work counts replace timing assertions."""

import unittest
from unittest import mock

from hypothesis import given, settings
from hypothesis import strategies as st

from applicant.domain.canonical import canonical_json
from applicant.domain.eligibility_engine import evaluate_eligibility
from applicant.domain.ranking_facts import RankingFacts, WorkRoute
from applicant.domain.ranking_v2 import ScoringConfigurationV2
from applicant.domain.work_routes import _location, evaluate_routes
from tests.ranking_fixtures import NOW, compensation_fixture
from tests.test_eligibility import candidate, employment


def route(
    key='r',
    *,
    mode='Remote',
    scope='worldwide',
    countries=(),
    residual=(),
    quality='explicit',
    source_id=None,
):
    return WorkRoute.model_validate(
        {
            'route_id': key,
            'source': compensation_fixture()['source']
            | {'evidence_id': source_id or key, 'quality': quality},
            'mode': mode,
            'remote_scope': scope,
            'allowed_countries': countries,
            'residual_restrictions': residual,
        }
    )


def evaluate(routes, *, person=None, complete=True, extra=()):
    return evaluate_routes(
        candidate() if person is None else person,
        (RankingFacts(routes=routes, routes_complete=complete), *extra),
        config=ScoringConfigurationV2(),
        evaluated_at=NOW,
    )


class WorkRoutesTest(unittest.TestCase):
    def test_mode_location_route_matrix(self):
        person = candidate(work_country='IN', required_work_modes=('Remote',))
        for offered, expected in (
            (route(), 'pass'),
            (route(scope='country_list', countries=('US',)), 'fail'),
            (route(scope='country_list', countries=('IN',)), 'pass'),
            (route(scope='unknown'), 'unknown'),
            (route(residual=('US timezone overlap',)), 'unknown'),
            (
                route(scope='country_list', countries=('US',), residual=('authorization unknown',)),
                'fail',
            ),
            (
                route(scope='country_list', countries=('IN',), residual=('authorization unknown',)),
                'unknown',
            ),
            (route(mode='Onsite', scope='unknown'), 'fail'),
            (route(quality='inferred'), 'unknown'),
        ):
            with self.subTest(route=offered):
                self.assertEqual(evaluate((offered,), person=person).criterion.status, expected)
        self.assertEqual(
            evaluate((route(scope='country_list', countries=('US',)),)).criterion.status, 'unknown'
        )
        self.assertEqual(evaluate(()).criterion.status, 'unknown')
        self.assertEqual(
            evaluate((route(mode='Onsite', scope='unknown'),)).criterion.status, 'pass'
        )
        self.assertEqual(
            evaluate(
                (route(scope='country_list', countries=('US',)),), person=person, complete=False
            ).criterion.status,
            'unknown',
        )

    def test_alternative_routes_are_joint_not_independent_marginal_passes(self):
        person = candidate(work_country='IN', required_work_modes=('Remote',))
        routes = (
            route('remote', scope='country_list', countries=('US',)),
            route('onsite', mode='Onsite', scope='unknown'),
        )
        self.assertEqual(evaluate(routes, person=person).criterion.status, 'fail')
        person = candidate(work_country='IN')
        self.assertEqual(evaluate(routes, person=person).criterion.status, 'pass')

    def test_conflicts_classified_before_short_circuit_and_retained(self):
        first = route('remote', scope='country_list', countries=('US',), source_id='structured')
        second = route('remote', source_id='description')
        extra = (RankingFacts(routes=(second,), routes_complete=True),)
        result = evaluate((first,), extra=extra)
        self.assertEqual(result.criterion.status, 'unknown')
        self.assertIn('worldwide', result.criterion.explanation)
        self.assertIn('US', result.criterion.explanation)
        self.assertEqual(type(result).model_validate_json(canonical_json(result)), result)
        alternate = route('onsite', mode='Onsite', scope='unknown')
        result = evaluate((alternate, first), extra=extra)
        self.assertEqual(result.criterion.status, 'pass')
        self.assertEqual(result.checks[-1].status, 'not_evaluated')
        self.assertTrue(result.checks[-1].conflicts)
        # Mode disagreement on the same route ID also needs review.
        extra = (
            RankingFacts(
                routes=(route('remote', mode='Onsite', scope='unknown', source_id='second'),)
            ),
        )
        self.assertEqual(evaluate((first,), extra=extra).criterion.status, 'unknown')

    def test_repeated_assertions_and_residual_restrictions(self):
        known = route(source_id='known')
        unknown = route(scope='unknown', source_id='unknown')
        extra = (RankingFacts(routes=(unknown,), routes_complete=True),)
        self.assertEqual(evaluate((known,), extra=extra).criterion.status, 'pass')
        restricted = route(residual=('authorization unknown',), source_id='restricted')
        extra = (RankingFacts(routes=(restricted,), routes_complete=True),)
        self.assertEqual(evaluate((known,), extra=extra).criterion.status, 'unknown')

    def test_short_circuit_work_counts_on_large_alternative_sets(self):
        routes = tuple(route(str(index)) for index in range(200))
        with mock.patch('applicant.domain.work_routes._location', wraps=_location) as location:
            result = evaluate(routes)
        self.assertEqual(location.call_count, 1)
        self.assertEqual(sum(c.status == 'not_evaluated' for c in result.checks), 199)
        person = candidate(required_work_modes=('Onsite',))
        with mock.patch('applicant.domain.work_routes._location', wraps=_location) as location:
            self.assertEqual(evaluate(routes, person=person).criterion.status, 'fail')
        self.assertEqual(location.call_count, 0)

    def test_all_job_criteria_and_preference_notes_survive_salary_exclusion(self):
        person = candidate(
            minimum_base_salary=100,
            employment_preference='permanent_only',
            preferred_regions=('Europe',),
        )
        from tests.test_normalization import normalize, salary

        pay = normalize(salary(minimum=0, maximum=0, shape='exact'))
        bundle = RankingFacts(
            routes=(route(scope='unknown'),), employment=(employment(('contract',), complete=True),)
        )
        result = evaluate_eligibility(
            person, (bundle,), compensation=pay, config=ScoringConfigurationV2(), evaluated_at=NOW
        )
        self.assertEqual(result.decision.status, 'excluded')
        self.assertEqual(len(result.decision.criteria), 3)
        self.assertEqual(result.decision.criteria[1].status, 'fail')
        self.assertEqual(result.decision.criteria[2].status, 'unknown')
        self.assertEqual(result.notes, ('unstructured_preference_not_evaluated',))

    @settings(max_examples=60)
    @given(
        st.lists(
            st.tuples(
                st.sampled_from(('Remote', 'Onsite')),
                st.sampled_from(('worldwide', 'unknown', 'IN', 'US')),
            ),
            max_size=12,
        ),
        st.booleans(),
    )
    def test_decision_matches_exhaustive_reference_under_permutations(self, specs, complete):
        person = candidate(work_country='IN', required_work_modes=('Remote',))
        offered = []
        outcomes = []
        for index, (mode, scope) in enumerate(specs):
            countries = (scope,) if scope in ('IN', 'US') else ()
            offered.append(
                route(
                    str(index),
                    mode=mode,
                    scope='unknown' if mode == 'Onsite' else 'country_list' if countries else scope,
                    countries=countries if mode == 'Remote' else (),
                )
            )
            outcomes.append(
                'fail'
                if mode != 'Remote' or scope == 'US'
                else 'unknown'
                if scope == 'unknown'
                else 'pass'
            )
        expected = (
            'pass'
            if 'pass' in outcomes
            else 'unknown'
            if not outcomes or 'unknown' in outcomes or not complete
            else 'fail'
        )
        self.assertEqual(
            evaluate(tuple(offered), person=person, complete=complete).criterion.status, expected
        )
        self.assertEqual(
            evaluate(tuple(reversed(offered)), person=person, complete=complete).criterion.status,
            expected,
        )
