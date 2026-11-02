"""Conservative salary prose parsing uses no inferred units or float arithmetic."""

import unittest
from decimal import Decimal, Inexact, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st

from applicant.domain.ranking_facts import FactSource
from applicant.domain.ranking_salary import decimal_amount, parse_compensation
from tests.ranking_fixtures import compensation_facts as salary
from tests.ranking_fixtures import compensation_fixture
from tests.test_normalization import normalize


def parse(text, **kwargs):
    return parse_compensation(text, source=FactSource(**compensation_fixture()['source']), **kwargs)


class RankingSalaryTest(unittest.TestCase):
    def test_explicit_salary_formats(self):
        for text, low, high, currency, period in (
            ('USD 5,000–6,000 monthly gross base', '5000', '6000', 'USD', 'monthly'),
            ('GBP 25/hour gross base', '25', '25', 'GBP', 'hourly'),
            ('EUR 45.000,50 annual gross base', '45000.50', '45000.50', 'EUR', 'annual'),
            ('CHF 110’000 yearly net base', '110000', '110000', 'CHF', 'annual'),
            ('INR 2.5-3 Lacs PA gross base', '250000', '300000', 'INR', 'annual'),
            (
                '€80–100k annual gross base plus bonus 20k and 401k',
                '80000',
                '100000',
                'EUR',
                'annual',
            ),
            ('US$ 0 annual gross base', '0', '0', 'USD', 'annual'),
        ):
            with self.subTest(text=text):
                result = parse(text)
                self.assertEqual(result.warnings, ())
                facts = result.facts[0]
                self.assertEqual((facts.minimum, facts.maximum), (Decimal(low), Decimal(high)))
                self.assertEqual((facts.currency, facts.period), (currency, period))
                for start, end in result.matched_spans:
                    self.assertTrue(any(character.isdigit() for character in text[start:end]))

    def test_open_bounds_and_plus_suffix(self):
        for text, shape in (
            ('from EUR 80k', 'lower_only'),
            ('up to EUR 100k', 'upper_only'),
            ('EUR 80k+', 'lower_only'),
        ):
            result = parse(text + ' annual gross base')
            self.assertEqual(result.facts[0].shape, shape)
            self.assertIsNone(normalize(result.facts[0]).representative_value)

    def test_ambiguous_and_incomplete_metadata_stays_unknown(self):
        for text, field, reason in (
            ('$100 annual gross base', 'currency', 'salary_currency_unknown'),
            ('¥100 annual gross base', 'currency', 'salary_currency_unknown'),
            ('USD EUR 100 annual gross base', 'currency', 'salary_currency_conflicting'),
            ('EUR 100 gross base', 'period', 'salary_period_unknown'),
            ('EUR 100 annual monthly gross base', 'period', 'salary_period_conflicting'),
            ('EUR 100 annual base', 'basis', 'salary_basis_unknown'),
            ('EUR 100 annual net gross base', 'basis', 'salary_basis_conflicting'),
            ('EUR 100 annual gross salary', 'component', 'salary_base_component_unknown'),
            ('EUR 100 annual gross total compensation', 'component', 'total_pay_not_base'),
        ):
            with self.subTest(text=text):
                result = parse(text)
                self.assertIn(reason, result.warnings)
                self.assertIn(getattr(result.facts[0], field), (None, 'unknown', 'total'))
                self.assertEqual(normalize(result.facts[0]).status, 'review_required')

    def test_unsupported_or_invalid_bounds_need_review(self):
        for text, reason in (
            ('EUR 100-80k annual gross base', 'salary_range_reversed'),
            ('EUR -100 annual gross base', 'salary_negative_or_accounting_amount'),
            ('-€100 annual gross base', 'salary_negative_or_accounting_amount'),
            ('EUR 100--200 annual gross base', 'salary_negative_or_accounting_amount'),
            ('EUR 1e3 annual gross base', 'salary_numeric_format_unsupported'),
            ('from EUR 100-200 annual gross base', 'salary_bounds_ambiguous'),
            ('from up to EUR 100 annual gross base', 'salary_bounds_ambiguous'),
            (
                'Senior EUR 80k; Staff EUR 100k annual gross base',
                'salary_tiers_or_amounts_ambiguous',
            ),
            ('EUR 1 / 2 / 3 annual gross base', 'salary_tiers_or_amounts_ambiguous'),
            ('Negotiable', 'salary_bounds_unknown'),
            ('9' * 7000 + 'k annual gross base', 'salary_numeric_range'),
            ('EUR 123456789012345678901234567890123456 annual gross base', 'salary_numeric_range'),
            ('EUR 20% annual gross base', 'salary_percentage_unsupported'),
        ):
            with self.subTest(reason=reason):
                result = parse(text)
                self.assertFalse(result.facts)
                self.assertIn(reason, result.warnings)

    def test_absent_long_and_structured_tiers(self):
        for text in ('', '   ', None):
            self.assertEqual(parse(text).warnings, ('salary_missing',))
        self.assertEqual(parse('x' * 20001).warnings, ('salary_text_too_long',))
        tiers = (salary(tier='Senior'), salary(tier='Staff'))
        result = parse('Ambiguous text ignored in favor of structured facts', structured=tiers)
        self.assertEqual(result.facts, tiers)
        self.assertEqual(result.matched_spans, ())

    def test_decimal_locale_tokens_and_context_isolation(self):
        for text, scaled, value in (
            ('1,200.25', False, '1200.25'),
            ('1.200,25', False, '1200.25'),
            ('1 200', False, '1200'),
            ('1\u00a0200', False, '1200'),
            ('1\u202f200', False, '1200'),
            ('12,00,000', False, '1200000'),
            ('2,5', True, '2.5'),
            ('2.500', True, '2.500'),
            ('45.000', False, '45000'),
        ):
            self.assertEqual(decimal_amount(text, scaled=scaled), Decimal(value))
        with localcontext() as context:
            context.prec = 2
            context.traps[Inexact] = True
            self.assertEqual(
                parse('USD 12.345k annual gross base').facts[0].minimum, Decimal('12345')
            )

    @settings(max_examples=40)
    @given(st.integers(min_value=0, max_value=1_000_000))
    def test_exact_plain_integer_amounts_round_trip(self, amount):
        facts = parse(f'EUR {amount} annual gross base').facts[0]
        self.assertEqual(facts.minimum, amount)
        self.assertEqual(facts.maximum, amount)
