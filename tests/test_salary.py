"""parse_salary is the densest logic in the project and decides which jobs a user
ever sees, so a wrong multiplier is a 100x error in what gets filtered out."""

from __future__ import annotations

import unittest

from applicant.domain.salary import parse_salary


class ParseSalaryTest(unittest.TestCase):
    def test_indian_lakh_range(self):
        salary = parse_salary('2-2.5 Lacs PA')
        assert salary is not None
        self.assertEqual(salary.low, 200_000)
        self.assertEqual(salary.high, 250_000)
        self.assertEqual(salary.currency, 'INR')
        self.assertEqual(salary.annual_high, 250_000)

    def test_range_unit_only_on_the_last_number(self):
        """'2-2.5 Lacs' means 2 lakh to 2.5 lakh, not 2 rupees to 2.5 lakh."""
        salary = parse_salary('2-2.5 Lacs PA')
        assert salary is not None
        self.assertEqual(salary.low, 200_000)

    def test_crore(self):
        salary = parse_salary('1-1.5 Cr PA')
        assert salary is not None
        self.assertEqual(salary.low, 10_000_000)
        self.assertEqual(salary.high, 15_000_000)
        self.assertEqual(salary.currency, 'INR')

    def test_monthly_is_annualised(self):
        salary = parse_salary('₹25K–₹40K a month')
        assert salary is not None
        self.assertEqual(salary.currency, 'INR')
        self.assertEqual(salary.period_per_year, 12)
        self.assertEqual(salary.annual_low, 300_000)
        self.assertEqual(salary.annual_high, 480_000)

    def test_hourly_is_annualised(self):
        salary = parse_salary('$30 an hour')
        assert salary is not None
        self.assertEqual(salary.currency, 'USD')
        self.assertEqual(salary.period_per_year, 2080)
        self.assertEqual(salary.annual_high, 62_400)

    def test_annual_range_with_thousands_separators(self):
        salary = parse_salary('£45,000 - £55,000 per annum')
        assert salary is not None
        self.assertEqual(salary.currency, 'GBP')
        self.assertEqual(salary.low, 45_000)
        self.assertEqual(salary.high, 55_000)
        self.assertEqual(salary.period_per_year, 1)

    def test_euro_is_recognised(self):
        salary = parse_salary('€60.000 per year')
        assert salary is not None
        self.assertEqual(salary.currency, 'EUR')

    def test_en_dash_and_minus_sign_are_both_ranges(self):
        for text in ('$50,000–$60,000 a year', '$50,000−$60,000 a year'):
            with self.subTest(text=text):
                salary = parse_salary(text)
                assert salary is not None
                self.assertEqual(salary.low, 50_000)
                self.assertEqual(salary.high, 60_000)

    def test_unpriced_postings_are_unknown_not_zero(self):
        for text in ('Not disclosed', 'not Disclosed', 'Unpaid', 'Negotiable'):
            with self.subTest(text=text):
                self.assertIsNone(parse_salary(text))

    def test_empty_input(self):
        self.assertIsNone(parse_salary(None))
        self.assertIsNone(parse_salary(''))

    def test_no_numbers_at_all(self):
        self.assertIsNone(parse_salary('Competitive salary'))

    def test_a_bare_year_is_not_pay(self):
        """A posting that only says '2024' must not read as a 2024-rupee salary."""
        self.assertIsNone(parse_salary('2024'))
        self.assertIsNone(parse_salary('1999'))

    def test_a_bare_year_with_a_currency_is_pay(self):
        salary = parse_salary('$2024')
        assert salary is not None
        self.assertEqual(salary.high, 2024)

    def test_annual_properties_are_none_when_unset(self):
        salary = parse_salary('Not disclosed')
        self.assertIsNone(salary)


class LocaleTest(unittest.TestCase):
    """Indeed answers from fifteen country sites: pay as each of them writes it."""

    CASES = (
        # text, low, high, currency, periods per year
        ('45.000 € - 55.000 € pro Jahr', 45_000, 55_000, 'EUR', 1),
        ('45 000 € par an', 45_000, 45_000, 'EUR', 1),
        ('45\u202f000 à 55\u202f000 € par an', 45_000, 55_000, 'EUR', 1),
        ('3.500 € pro Monat', 3_500, 3_500, 'EUR', 12),
        ('3.500,50 € pro Monat', 3_500.5, 3_500.5, 'EUR', 12),
        ('18,00 € pro Stunde', 18, 18, 'EUR', 2080),
        ("CHF 110'000", 110_000, 110_000, 'CHF', 1),
        ('zł 12 000 miesięcznie', 12_000, 12_000, 'PLN', 12),
        ('R$ 4.500 por mês', 4_500, 4_500, 'BRL', 12),
        ('¥5,000,000 a year', 5_000_000, 5_000_000, 'JPY', 1),
        ('CA$ 80,000 a year', 80_000, 80_000, 'CAD', 1),
        ('AUD 120,000 a year', 120_000, 120_000, 'AUD', 1),
        ('SGD 5,000 a month', 5_000, 5_000, 'SGD', 12),
        ('US$ 90,000', 90_000, 90_000, 'USD', 1),
        # and the ones every existing board already writes, unchanged
        ('₹12,00,000 - ₹15,00,000 a year', 1_200_000, 1_500_000, 'INR', 1),
        ('₹ 3,50,000 - 5,00,000 P.A.', 350_000, 500_000, 'INR', 1),
        ('10-15 LPA', 1_000_000, 1_500_000, 'INR', 1),
        ('2,5 Lacs', 250_000, 250_000, 'INR', 1),
        ('$18.00 - $22.00 an hour', 18, 22, 'USD', 2080),
        ('$100K-$120K', 100_000, 120_000, 'USD', 1),
    )

    def test_each_format(self):
        for text, low, high, currency, periods in self.CASES:
            with self.subTest(text=text):
                salary = parse_salary(text)
                assert salary is not None
                self.assertEqual(
                    (salary.low, salary.high, salary.currency, salary.period_per_year),
                    (low, high, currency, periods),
                )

    def test_only_the_first_figure_or_range_is_pay(self):
        """'plus 401k' is not the top of the range, and does not rescale it."""
        salary = parse_salary('$120,000 - $150,000 a year, plus 401k')
        assert salary is not None
        self.assertEqual((salary.low, salary.high), (120_000, 150_000))
        salary = parse_salary('₹10L - ₹12L + bonus 2L')
        assert salary is not None
        self.assertEqual((salary.low, salary.high), (1_000_000, 1_200_000))

    def test_a_lower_case_word_is_not_a_currency_code(self):
        salary = parse_salary('50,000 a year, try to negotiate')
        assert salary is not None
        self.assertIsNone(salary.currency)


if __name__ == '__main__':
    unittest.main()
