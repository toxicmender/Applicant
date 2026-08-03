"""Cross-currency salary comparison.

Every test here runs offline against a fixed table. Nothing reaches the ECB or
the World Bank, so a change in real rates can never turn the suite red.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

from applicant.money import PPP_SEED, Rates, convert


class OfflineRates(Rates):
    """A table with known numbers, so the arithmetic is checkable by hand."""

    FX: ClassVar[dict[str, float]] = {'USD': 1.0, 'INR': 80.0, 'GBP': 0.80, 'EUR': 0.90}
    PPP: ClassVar[dict[str, float]] = {'USD': 1.0, 'INR': 20.0, 'GBP': 0.70}

    def __init__(self):
        super().__init__(path='/nonexistent/money-cache.json', offline=True)

    def fx(self, base='USD'):
        return dict(self.FX) if base == 'USD' else {}

    def ppp(self, currency):
        return self.PPP.get((currency or '').upper())


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.table = OfflineRates()

    def test_the_same_currency_is_returned_untouched(self):
        self.assertEqual(convert(100.0, 'INR', 'INR', table=self.table), (100.0, None))

    def test_market_uses_the_exchange_rate(self):
        # ₹1,600,000 at 80 to the dollar
        amount, note = convert(1_600_000, 'INR', 'USD', basis='market', table=self.table)
        assert amount is not None
        self.assertAlmostEqual(amount, 20_000.0)
        self.assertIsNone(note)

    def test_ppp_uses_purchasing_power(self):
        # ₹1,600,000 / 20 = $80,000 international, which is four times the market
        # figure - that difference is the whole point of the default
        amount, note = convert(1_600_000, 'INR', 'USD', basis='ppp', table=self.table)
        assert amount is not None
        self.assertAlmostEqual(amount, 80_000.0)
        self.assertIsNone(note)

    def test_ppp_between_two_non_dollar_currencies(self):
        amount, note = convert(1_600_000, 'INR', 'GBP', basis='ppp', table=self.table)
        assert amount is not None
        self.assertAlmostEqual(amount, 56_000.0)
        self.assertIsNone(note)

    def test_a_missing_ppp_factor_degrades_to_market_and_says_so(self):
        """Nothing is ever invented - the caller is told the basis changed."""
        amount, note = convert(900.0, 'EUR', 'USD', basis='ppp', table=self.table)
        assert amount is not None
        self.assertAlmostEqual(amount, 1000.0)
        self.assertEqual(note, 'ppp-unavailable')

    def test_an_unknown_currency_is_reported_not_guessed(self):
        amount, note = convert(100.0, 'ISK', 'USD', basis='market', table=self.table)
        self.assertIsNone(amount)
        self.assertEqual(note, 'rate-unavailable')

    def test_missing_inputs(self):
        self.assertEqual(convert(None, 'INR', 'USD', table=self.table), (None, 'currency-unknown'))
        self.assertEqual(convert(100.0, None, 'USD', table=self.table), (None, 'currency-unknown'))
        self.assertEqual(convert(100.0, 'INR', '', table=self.table), (None, 'currency-unknown'))

    def test_currency_codes_are_case_insensitive(self):
        amount, _ = convert(1_600_000, 'inr', 'usd', basis='market', table=self.table)
        assert amount is not None
        self.assertAlmostEqual(amount, 20_000.0)


class RatesCacheTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / 'cache.json')

    def test_the_seed_is_available_without_a_network(self):
        table = Rates(path=self.path, offline=True)
        self.assertEqual(table.ppp('USD'), 1.0)
        self.assertEqual(table.ppp('INR'), PPP_SEED['IND']['value'])

    def test_the_us_dollar_is_the_international_dollar(self):
        self.assertEqual(PPP_SEED['USA']['value'], 1.0)

    def test_a_currency_with_no_country_mapping_is_unknown(self):
        table = Rates(path=self.path, offline=True)
        self.assertIsNone(table.ppp('ISK'))
        self.assertIsNone(table.ppp(None))

    def test_offline_never_invents_a_missing_factor(self):
        table = Rates(path=self.path, offline=True)
        self.assertIsNone(table.ppp('SEK'))
        self.assertEqual(table.fx('USD'), {})

    def test_a_cached_value_is_read_back(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(
                {'fx': {'USD': {'rates': {'USD': 1.0, 'INR': 83.0}, 'fetched': 9e12}}, 'ppp': {}},
                handle,
            )

        table = Rates(path=self.path, offline=True)
        self.assertEqual(table.fx('USD')['INR'], 83.0)

    def test_a_corrupt_cache_is_not_fatal(self):
        Path(self.path).write_text('{not json', encoding='utf-8')
        table = Rates(path=self.path, offline=True)
        self.assertEqual(table.ppp('USD'), 1.0)


if __name__ == '__main__':
    unittest.main()
