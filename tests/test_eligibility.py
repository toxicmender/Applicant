"""Salary/employment boundary tables and conflict explanations are independent of fit."""

import unittest
from decimal import Decimal, Inexact, localcontext

from applicant.domain.canonical import canonical_json
from applicant.domain.eligibility import (
    compensation_eligibility,
    employment_eligibility,
    salary_floor,
)
from applicant.domain.ranking_facts import EmploymentOptions
from applicant.domain.ranking_v2 import CandidateProfileV2, ScoringConfigurationV2
from tests.ranking_fixtures import NOW, compensation_fixture
from tests.test_normalization import normalize, salary
from tests.test_ranking_contracts import profile


def candidate(**changes):
    return CandidateProfileV2.model_validate(
        profile().model_dump()
        | {
            'schema_version': 2,
            'employment_preference': 'either',
        }
        | changes
    )


def employment(arrangements=(), *, complete=False, source_id='a', quality='explicit'):
    return EmploymentOptions.model_validate(
        {
            'source': compensation_fixture()['source']
            | {'evidence_id': source_id, 'quality': quality},
            'arrangements': arrangements,
            'complete': complete,
        }
    )


class EligibilityTest(unittest.TestCase):
    def test_floor_boundary_table_and_zero_weight_independence(self):
        person = candidate(minimum_base_salary='100')
        for low, high, shape, expected in (
            (100, 200, 'range', 'pass'),
            (50, 99, 'range', 'fail'),
            (50, 100, 'range', 'unknown'),
            (100, None, 'lower_only', 'pass'),
            (50, None, 'lower_only', 'unknown'),
            (None, 100, 'upper_only', 'unknown'),
            (None, 99, 'upper_only', 'fail'),
            (100, 100, 'exact', 'pass'),
        ):
            with self.subTest(low=low, high=high):
                pay = normalize(
                    salary(minimum=low, maximum=high, shape=shape, currency='EUR', period='annual')
                )
                self.assertEqual(salary_floor(person, pay).status, expected)
        weights = dict.fromkeys(person.weights, Decimal(0)) | {'technical_fit': Decimal(1)}
        person = candidate(minimum_base_salary=100, weights=weights)
        self.assertEqual(
            salary_floor(person, normalize(salary(minimum=0, maximum=0, shape='exact'))).status,
            'fail',
        )

    def test_salary_unknown_units_policy_and_numeric_range(self):
        self.assertEqual(salary_floor(candidate(), None).status, 'not_applicable')
        person = candidate(minimum_base_salary=100)
        self.assertEqual(salary_floor(person, None).status, 'unknown')
        pay = normalize(salary(currency='EUR'))
        for changes in (
            {'evidence_quality': 0},
            {'normalized_currency': 'USD'},
            {'normalized_basis': 'net', 'original_basis': 'net'},
        ):
            changed = type(pay).model_validate(pay.model_dump() | changes)
            self.assertEqual(salary_floor(person, changed).status, 'unknown')
        unknown = normalize(salary(basis=None))
        self.assertEqual(salary_floor(person, unknown).status, 'unknown')
        self.assertEqual(
            salary_floor(candidate(minimum_base_salary='1e40'), pay).reason_code,
            'salary_numeric_range',
        )

    def test_salary_rounding_and_ambient_context(self):
        person = candidate(minimum_base_salary='100.005')
        pay = normalize(
            salary(minimum=100, maximum=100, shape='exact', period='annual', currency='EUR')
        )
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            self.assertEqual(salary_floor(person, pay).status, 'pass')

    def test_employment_acceptance_completeness_and_untrusted_sources(self):
        config = ScoringConfigurationV2()

        def evaluate(person, options):
            return employment_eligibility(person, options, config=config, evaluated_at=NOW)

        self.assertEqual(evaluate(candidate(), ()).status, 'not_applicable')
        person = candidate(employment_preference='permanent_only')
        self.assertEqual(evaluate(person, ()).status, 'unknown')
        for options, expected in (
            ((employment(('permanent',)),), 'pass'),
            ((employment(('contract',), complete=True),), 'fail'),
            ((employment(('contract',)),), 'unknown'),
            ((employment(('permanent',), quality='inferred', complete=True),), 'unknown'),
            ((employment(('permanent', 'contract'), complete=True),), 'pass'),
        ):
            self.assertEqual(evaluate(person, options).status, expected)
        person = candidate(employment_preference='contract_only')
        self.assertEqual(evaluate(person, (employment(('contract',)),)).status, 'pass')

    def test_employment_conflicts_are_readable_and_round_trip(self):
        options = (
            employment(('contract',), complete=True, source_id='one'),
            employment(('permanent',), complete=True, source_id='two'),
        )
        decision = employment_eligibility(
            candidate(employment_preference='permanent_only'),
            options,
            config=ScoringConfigurationV2(),
            evaluated_at=NOW,
        )
        self.assertEqual(decision.status, 'unknown')
        self.assertEqual(decision.reason_code, 'conflicting_evidence')
        self.assertIn('contract', decision.explanation)
        self.assertIn('permanent', decision.explanation)
        self.assertTrue(decision.conflicts[0].needs_verification)
        self.assertEqual(type(decision).model_validate_json(canonical_json(decision)), decision)

    def test_future_evidence_is_invalid_caller_input(self):
        from datetime import timedelta

        option = employment(('contract',))
        option = EmploymentOptions.model_validate(
            option.model_dump()
            | {
                'source': option.source.model_dump() | {'observed_at': NOW + timedelta(days=1)},
            }
        )
        with self.assertRaises(ValueError):
            employment_eligibility(
                candidate(employment_preference='contract_only'),
                (option,),
                config=ScoringConfigurationV2(),
                evaluated_at=NOW,
            )

    def test_salary_assertions_conflicts_tiers_and_normalization_coherence(self):
        person = candidate(minimum_base_salary=100)
        config = ScoringConfigurationV2()
        first = salary()
        second = salary(
            minimum=8000,
            maximum=9000,
            source=compensation_fixture()['source'] | {'evidence_id': 'second'},
        )

        def evaluate(assertions, **kwargs):
            return compensation_eligibility(
                person, normalize(first), assertions, config=config, evaluated_at=NOW, **kwargs
            )

        self.assertEqual(evaluate((first, second)).reason_code, 'conflicting_evidence')
        self.assertEqual(evaluate((first,)).status, 'pass')
        self.assertEqual(
            evaluate(
                (salary(source=compensation_fixture()['source'] | {'quality': 'inferred'}),)
            ).reason_code,
            'salary_evidence_unsupported',
        )
        self.assertEqual(evaluate((second,)).reason_code, 'salary_normalization_mismatch')
        self.assertEqual(evaluate((salary(basis=None),)).reason_code, 'salary_evidence_incomplete')
        self.assertEqual(
            evaluate((first,), selected_tier='missing').reason_code, 'salary_evidence_unsupported'
        )
        senior = salary(tier='Senior')
        staff = salary(tier='Staff')
        self.assertEqual(evaluate((senior, staff)).reason_code, 'salary_tier_selection_required')
        self.assertEqual(evaluate((senior, staff), selected_tier='Senior').status, 'pass')

    def test_repeated_conflicting_evidence_uses_stable_bounded_witnesses(self):
        values = (
            employment(('contract',), complete=True, source_id='z'),
            employment(('contract',), complete=True, source_id='a'),
            employment(('contract',), complete=True, source_id='y'),
            employment(('permanent',), complete=True, source_id='b'),
        )
        decision = employment_eligibility(
            candidate(employment_preference='contract_only'),
            values,
            config=ScoringConfigurationV2(),
            evaluated_at=NOW,
        )
        self.assertEqual({a.evidence_id for a in decision.conflicts[0].assertions}, {'a', 'b'})
        self.assertEqual(len(decision.conflicts[0].assertions), 2)
