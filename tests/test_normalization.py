"""Independently calculated examples and bounded invariants for base-pay conversion."""

import unittest
from decimal import ROUND_DOWN, Decimal, Inexact, localcontext
from typing import cast

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from applicant.domain.canonical import canonical_json
from applicant.domain.contracts import Basis
from applicant.domain.normalization import normalize_compensation
from applicant.domain.ranking_facts import CompensationFacts
from applicant.domain.ranking_v2 import ScoringConfigurationV2
from tests.ranking_fixtures import compensation_facts as salary
from tests.ranking_fixtures import compensation_fixture
from tests.test_ranking_contracts import fx


def normalize(facts=None, *, snapshot=None, config=None, target='EUR', basis='gross'):
    return normalize_compensation(
        salary() if facts is None else facts,
        target_currency=target,
        target_basis=cast(Basis, basis),
        fx=fx(rates=compensation_fixture()['fx']) if snapshot is None else snapshot,
        config=ScoringConfigurationV2() if config is None else config,
    )


class NormalizationTest(unittest.TestCase):
    def test_golden_tiers_and_cross_currency_values(self):
        fixture = compensation_fixture()
        for case in fixture['cases']:
            with self.subTest(case=case['id']):
                result = normalize(
                    CompensationFacts.model_validate(case['facts'] | {'source': fixture['source']})
                )
                expected = case['expected_eur']
                for field, key in (
                    ('annual_minimum', 'minimum'),
                    ('annual_maximum', 'maximum'),
                    ('representative_value', 'midpoint'),
                ):
                    value = expected[key]
                    self.assertEqual(
                        getattr(result, field), None if value is None else Decimal(value)
                    )
                self.assertEqual(result.status, 'available')
                self.assertEqual(type(result).model_validate_json(canonical_json(result)), result)

    def test_all_periods_and_custom_assumptions(self):
        config = ScoringConfigurationV2.model_validate(
            {'annual_hours': '1800', 'annual_days': '220'}
        )
        for period, expected in (
            ('annual', 10),
            ('monthly', 120),
            ('weekly', 520),
            ('daily', 2200),
            ('hourly', 18000),
        ):
            with self.subTest(period=period):
                result = normalize(
                    salary(minimum=10, maximum=10, shape='exact', currency='EUR', period=period),
                    config=config,
                )
                self.assertEqual(result.annual_minimum, expected)
                self.assertEqual(result.assumed_annual_hours, 1800 if period == 'hourly' else None)
                self.assertEqual(result.assumed_annual_days, 220 if period == 'daily' else None)
                self.assertEqual(result.scoring_config_id, config.scoring_config_id)

    def test_strategies_and_open_ranges_do_not_invent_bounds(self):
        for strategy in ('minimum', 'maximum', 'midpoint'):
            config = ScoringConfigurationV2.model_validate(
                {'representative_salary_strategy': strategy}
            )
            for shape, low, high in (('lower_only', 100, None), ('upper_only', None, 200)):
                with self.subTest(strategy=strategy, shape=shape):
                    result = normalize(
                        salary(
                            shape=shape, minimum=low, maximum=high, currency='EUR', period='annual'
                        ),
                        config=config,
                    )
                    expected = (
                        low if strategy == 'minimum' else high if strategy == 'maximum' else None
                    )
                    self.assertEqual(result.representative_value, expected)
                    self.assertEqual(result.annual_minimum, low)
                    self.assertEqual(result.annual_maximum, high)
                    self.assertEqual(
                        result.warnings,
                        ('representative_bound_missing',) if expected is None else (),
                    )

    def test_rounding_and_ambient_context_are_isolated(self):
        facts = salary(minimum='1.005', maximum='1.015', period='annual', currency='EUR')
        config = ScoringConfigurationV2()
        expected = normalize(facts, config=config)
        self.assertEqual(
            (expected.annual_minimum, expected.annual_maximum, expected.representative_value),
            (Decimal('1.00'), Decimal('1.02'), Decimal('1.01')),
        )
        with localcontext() as ambient:
            ambient.prec = 2
            ambient.rounding = ROUND_DOWN
            ambient.Emax = 2
            ambient.Emin = -2
            ambient.traps[Inexact] = True
            self.assertEqual(normalize(facts, config=config), expected)
            self.assertEqual(ambient.prec, 2)
            self.assertFalse(ambient.flags[Inexact])

    def test_missing_information_is_explained_without_comparable_values(self):
        for change, reason in (
            ({'minimum': None, 'maximum': None, 'shape': 'unknown'}, 'salary_bounds_unknown'),
            ({'currency': None}, 'salary_currency_unknown'),
            ({'period': None}, 'salary_period_unknown'),
            ({'basis': None}, 'salary_basis_unknown'),
            ({'basis': 'net'}, 'salary_basis_mismatch'),
            ({'component': 'total'}, 'total_pay_not_base'),
            ({'component': 'unknown'}, 'salary_base_component_unknown'),
            (
                {'source': compensation_fixture()['source'] | {'quality': 'unknown'}},
                'salary_evidence_unsupported',
            ),
        ):
            with self.subTest(reason=reason):
                result = normalize(salary(**change))
                self.assertEqual(result.status, 'review_required')
                self.assertIn(reason, result.warnings)
                self.assertIsNone(result.annual_maximum)
                self.assertEqual(result.evidence_quality, 0)

    def test_same_currency_identity_and_missing_cross_rates(self):
        identity = fx(provenance='identity', rates={'EUR': 1})
        self.assertEqual(normalize(salary(currency='EUR'), snapshot=identity).status, 'available')
        self.assertEqual(normalize(snapshot=identity).warnings, ('fx_rate_unavailable',))
        self.assertEqual(normalize(target='JPY').status, 'unavailable')
        self.assertEqual(normalize(salary(currency='JPY')).status, 'unavailable')
        # Quotes need not have the target as their base.
        result = normalize(
            salary(currency='GBP', minimum=80, maximum=80, shape='exact', period='annual'),
            target='USD',
        )
        self.assertEqual(result.representative_value, 125)

    def test_static_quality_and_opt_in(self):
        snapshot = fx(
            provenance='static', dataset_version='fixture', rates={'EUR': 1, 'USD': '1.25'}
        )
        self.assertEqual(normalize(snapshot=snapshot).warnings, ('static_fx_disabled',))
        result = normalize(snapshot=snapshot, config=ScoringConfigurationV2(allow_static_fx=True))
        self.assertEqual(result.evidence_quality, Decimal('.5'))
        self.assertEqual(normalize().evidence_quality, Decimal('.9'))
        self.assertEqual(normalize(salary(currency='EUR')).evidence_quality, 1)

    def test_zero_and_numeric_limits(self):
        self.assertEqual(
            normalize(salary(minimum=0, maximum=0, shape='exact')).representative_value, 0
        )
        for value in ('1e10000', '1e40'):
            result = normalize(salary(minimum=value, maximum=value, shape='exact'))
            self.assertEqual(result.warnings, ('normalization_numeric_range',))
        tiny = fx(rates={'EUR': 1, 'USD': '1e10000'})
        self.assertEqual(normalize(snapshot=tiny).warnings, ('normalization_numeric_range',))

    def test_invalid_caller_contracts_fail_before_calculation(self):
        with self.assertRaises(ValidationError):
            normalize(salary().model_copy(update={'minimum': Decimal(-1)}))
        with self.assertRaises(ValueError):
            normalize(target='ZZZ')
        with self.assertRaises(ValueError):
            normalize(basis='take-home')

    @settings(max_examples=40)
    @given(st.integers(min_value=0, max_value=10_000_000), st.integers(min_value=1, max_value=100))
    def test_conversion_is_monotone_and_keeps_range_order(self, amount, delta):
        low = Decimal(amount) / 100
        high = low + Decimal(delta)
        result = normalize(salary(minimum=low, maximum=high, period='annual'))
        assert result.annual_minimum is not None and result.annual_maximum is not None
        assert result.representative_value is not None
        self.assertLessEqual(result.annual_minimum, result.representative_value)
        self.assertLessEqual(result.representative_value, result.annual_maximum)
