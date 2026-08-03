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

import httpx

from applicant.money import (
    COUNTRY_CURRENCY,
    CURRENCY_COUNTRY,
    PPP_SEED,
    Rates,
    convert,
    fetch_factor,
    load_factors,
    refresh_factors,
)


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


def worldbank(rows):
    """The World Bank's two element response: metadata, then the series."""
    return httpx.Response(200, json=[{'page': 1}, rows])


class CurrencyMapTest(unittest.TestCase):
    def test_the_requested_currencies_are_mapped(self):
        for currency, country in (
            ('USD', 'USA'),
            ('GBP', 'GBR'),
            ('INR', 'IND'),
            ('NZD', 'NZL'),
            ('AUD', 'AUS'),
            ('EUR', 'EMU'),
            ('SEK', 'SWE'),
        ):
            with self.subTest(currency=currency):
                self.assertEqual(CURRENCY_COUNTRY[currency], country)

    def test_the_majors_are_covered(self):
        for currency in (
            'JPY',
            'CHF',
            'CAD',
            'CNY',
            'HKD',
            'SGD',
            'NOK',
            'DKK',
            'KRW',
            'PLN',
            'ZAR',
            'MXN',
            'BRL',
            'AED',
            'IDR',
            'THB',
        ):
            with self.subTest(currency=currency):
                self.assertIn(currency, CURRENCY_COUNTRY)

    def test_taiwan_is_absent_because_the_world_bank_has_no_series(self):
        """Mapping TWD would mean inventing a factor rather than fetching one."""
        self.assertNotIn('TWD', CURRENCY_COUNTRY)

    def test_country_codes_are_iso_3166_alpha_3(self):
        for currency, country in CURRENCY_COUNTRY.items():
            with self.subTest(currency=currency):
                self.assertEqual(len(country), 3, country)
                self.assertTrue(country.isupper())

    def test_the_reverse_map_covers_every_country(self):
        self.assertEqual(set(COUNTRY_CURRENCY), set(CURRENCY_COUNTRY.values()))


class LoadFactorsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / 'factors.json')

    def test_the_checked_in_file_is_what_seeds_the_table(self):
        self.assertEqual(load_factors(), PPP_SEED)

    def test_every_seeded_country_is_one_we_map(self):
        self.assertTrue(set(PPP_SEED) <= set(CURRENCY_COUNTRY.values()))

    def test_every_seeded_entry_states_its_year(self):
        for country, entry in PPP_SEED.items():
            with self.subTest(country=country):
                self.assertIsInstance(entry['value'], float)
                self.assertTrue(entry['year'])

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(load_factors(self.path), {})

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self):
        Path(self.path).write_text('{not json', encoding='utf-8')
        self.assertEqual(load_factors(self.path), {})

    def test_a_file_without_factors_reads_as_empty(self):
        Path(self.path).write_text('{"indicator": "PA.NUS.PPP"}', encoding='utf-8')
        self.assertEqual(load_factors(self.path), {})


class FetchFactorTest(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_the_newest_year_wins(self):
        def handler(request):
            return worldbank(
                [
                    {'date': '2021', 'value': 18.0},
                    {'date': '2025', 'value': 20.1},
                    {'date': '2023', 'value': 19.0},
                ]
            )

        self.assertEqual(
            fetch_factor('IND', client=self.client(handler), pause=0),
            {'value': 20.1, 'year': '2025'},
        )

    def test_null_values_are_skipped(self):
        def handler(request):
            return worldbank([{'date': '2025', 'value': None}, {'date': '2024', 'value': 19.0}])

        self.assertEqual(
            fetch_factor('IND', client=self.client(handler), pause=0),
            {'value': 19.0, 'year': '2024'},
        )

    def test_an_empty_series_is_a_real_answer_not_a_retry(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return worldbank([])

        self.assertIsNone(fetch_factor('XKX', client=self.client(handler), pause=0))
        self.assertEqual(len(calls), 1, 'no data is an answer, so it must not retry')

    def test_a_throttled_response_is_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            if len(calls) < 3:
                return httpx.Response(429, text='slow down')
            return worldbank([{'date': '2025', 'value': 20.1}])

        found = fetch_factor('IND', client=self.client(handler), pause=0, attempts=3)
        self.assertEqual(found, {'value': 20.1, 'year': '2025'})
        self.assertEqual(len(calls), 3)

    def test_giving_up_returns_none_rather_than_a_guess(self):
        def handler(request):
            raise httpx.ConnectError('blocked')

        self.assertIsNone(fetch_factor('GBR', client=self.client(handler), pause=0, attempts=2))


class RefreshFactorsTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / 'factors.json')
        Path(self.path).write_text(
            json.dumps({'factors': {'USA': {'value': 1.0, 'year': 'definition'}}}), encoding='utf-8'
        )

    def client(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def ok(self, request):
        return worldbank([{'date': '2025', 'value': 12.5}])

    def read(self):
        with open(self.path, encoding='utf-8') as handle:
            return json.load(handle)['factors']

    def test_a_fetched_factor_is_written_with_its_year(self):
        updated, failed, _ = refresh_factors(
            path=self.path, currencies=['GBP'], pause=0, client=self.client(self.ok)
        )

        self.assertEqual(updated, ['GBR'])
        self.assertEqual(failed, [])
        self.assertEqual(self.read()['GBR']['value'], 12.5)
        self.assertEqual(self.read()['GBR']['year'], '2025')
        self.assertIn('retrieved', self.read()['GBR'])

    def test_several_currencies_at_once(self):
        updated, _, _ = refresh_factors(
            path=self.path, currencies=['GBP', 'SEK', 'NZD'], pause=0, client=self.client(self.ok)
        )
        self.assertEqual(sorted(updated), ['GBR', 'NZL', 'SWE'])

    def test_the_dollar_is_never_fetched(self):
        """It is 1.0 by definition, not an observation."""
        calls = []

        def handler(request):
            calls.append(request.url)
            return self.ok(request)

        _, _, skipped = refresh_factors(
            path=self.path, currencies=['USD'], pause=0, client=self.client(handler)
        )
        self.assertEqual(skipped, ['USA'])
        self.assertEqual(calls, [])

    def test_known_factors_are_skipped_unless_forced(self):
        refresh_factors(path=self.path, currencies=['GBP'], pause=0, client=self.client(self.ok))

        updated, _, skipped = refresh_factors(
            path=self.path, currencies=['GBP'], pause=0, client=self.client(self.ok)
        )
        self.assertEqual(updated, [])
        self.assertEqual(skipped, ['GBR'])

    def test_force_refetches(self):
        refresh_factors(path=self.path, currencies=['GBP'], pause=0, client=self.client(self.ok))

        def newer(request):
            return worldbank([{'date': '2026', 'value': 13.0}])

        updated, _, _ = refresh_factors(
            path=self.path, currencies=['GBP'], force=True, pause=0, client=self.client(newer)
        )
        self.assertEqual(updated, ['GBR'])
        self.assertEqual(self.read()['GBR']['value'], 13.0)

    def test_a_failed_fetch_writes_nothing(self):
        """The whole point: a factor that could not be had is never invented."""
        before = Path(self.path).read_text(encoding='utf-8')

        def blocked(request):
            raise httpx.ConnectError('no network')

        updated, failed, _ = refresh_factors(
            path=self.path, currencies=['GBP'], pause=0, client=self.client(blocked)
        )

        self.assertEqual(updated, [])
        self.assertEqual(failed, ['GBR'])
        self.assertEqual(Path(self.path).read_text(encoding='utf-8'), before)

    def test_a_partial_run_keeps_what_it_got(self):
        def flaky(request):
            if 'SWE' in str(request.url):
                raise httpx.ConnectError('no network')
            return self.ok(request)

        updated, failed, _ = refresh_factors(
            path=self.path, currencies=['GBP', 'SEK'], pause=0, client=self.client(flaky)
        )

        self.assertEqual(updated, ['GBR'])
        self.assertEqual(failed, ['SWE'])
        self.assertIn('GBR', self.read())
        self.assertNotIn('SWE', self.read())

    def test_an_unknown_currency_is_ignored(self):
        updated, failed, skipped = refresh_factors(
            path=self.path, currencies=['XYZ'], pause=0, client=self.client(self.ok)
        )
        self.assertEqual((updated, failed, skipped), ([], [], []))

    def test_refreshing_everything_covers_every_mapped_country(self):
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return self.ok(request)

        updated, _, skipped = refresh_factors(path=self.path, pause=0, client=self.client(handler))
        self.assertEqual(len(set(updated) | set(skipped)), len(set(CURRENCY_COUNTRY.values())))

    def test_the_file_stays_sorted_so_diffs_stay_readable(self):
        refresh_factors(
            path=self.path, currencies=['SEK', 'GBP', 'NZD'], pause=0, client=self.client(self.ok)
        )
        countries = list(self.read())
        self.assertEqual(countries, sorted(countries))


if __name__ == '__main__':
    unittest.main()
