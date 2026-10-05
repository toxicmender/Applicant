"""The parsers that read scraped text, fuzzed with Hypothesis (tests/strategies.py).

A board can print anything in a salary, a date or a location field. Whatever
it prints, these must not raise, and what they return must keep the promises
the rest of the code relies on. Each failure Hypothesis once found is pinned
with @example, so it stays found.
"""

from __future__ import annotations

import string
import unittest
from datetime import date, datetime, timezone

from hypothesis import example, given
from hypothesis import strategies as st

from applicant import log
from applicant.domain import places
from applicant.domain.dates import epoch_to_iso, relative_to_iso
from applicant.domain.job import YEAR_LIMIT, experience_from, parse_experience
from applicant.domain.salary import CODES, PERIODS, parse_salary
from applicant.financials.parsing import parse_money, to_money
from applicant.storage import FORMULA_START, neutralise, restore
from tests.strategies import json_like, mutated, scraped
from tests.test_salary import LocaleTest

KNOWN_PAY = [case[0] for case in LocaleTest.CASES]
CURRENCY_CODES = set(CODES) | {
    'USD',
    'CAD',
    'AUD',
    'NZD',
    'SGD',
    'HKD',
    'BRL',
    'MXN',
    'INR',
    'EUR',
    'GBP',
    'JPY',
    'PLN',
}
PER_YEAR = {factor for _, factor in PERIODS}
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class SalaryFuzzTest(unittest.TestCase):
    @given(st.one_of(scraped(), mutated(KNOWN_PAY)))
    def test_any_text_is_a_salary_or_none_and_a_sane_one(self, text):
        salary = parse_salary(text)
        if salary is None:
            return
        self.assertIsNotNone(salary.low)
        assert salary.low is not None and salary.high is not None
        self.assertLessEqual(salary.low, salary.high)
        self.assertGreaterEqual(salary.low, 0)
        self.assertIn(salary.currency, CURRENCY_CODES | {None})
        self.assertIn(salary.period_per_year, PER_YEAR)


class DateFuzzTest(unittest.TestCase):
    @given(scraped())
    @example('99999999999999 days ago')
    @example('١٢ days ago')
    def test_any_text_is_a_date_not_after_today_or_none(self, text):
        found = relative_to_iso(text, now=NOW)
        if found is not None:
            self.assertLessEqual(date.fromisoformat(found), NOW.date())

    @given(st.one_of(st.integers(), st.floats(), st.none(), st.text(max_size=5), st.booleans()))
    def test_any_epoch_is_a_date_or_none(self, value):
        found = epoch_to_iso(value)
        if found is not None:
            date.fromisoformat(found)


class PlaceFuzzTest(unittest.TestCase):
    @given(scraped(), st.one_of(st.none(), scraped()))
    def test_any_two_places_compare_without_raising(self, wanted, actual):
        self.assertIn(places.within(wanted, actual), (True, False, None))
        self.assertIsInstance(places.countries_in(actual), set)

    @given(st.text(alphabet=string.ascii_letters + ' ', min_size=1, max_size=20).filter(str.strip))
    def test_a_place_is_within_itself(self, place):
        self.assertIs(places.within(place, place), True)


class MoneyFuzzTest(unittest.TestCase):
    @given(scraped())
    def test_any_text_is_money_or_none(self, text):
        money = parse_money(text)
        if money is not None and money.amount is not None:
            self.assertGreaterEqual(money.amount, 0)

    @given(json_like())
    def test_any_payload_shape_is_money_or_none(self, value):
        to_money(value)


class ExperienceFuzzTest(unittest.TestCase):
    @given(
        st.one_of(
            scraped(),
            st.from_regex(r'\d{1,3}(\.\d)?\s*(-|to)\s*\d{1,3}\s*(yrs|years)', fullmatch=True),
        )
    )
    @example('10-2 years')
    def test_any_text_is_a_range_inside_the_model_s_bounds_or_none(self, text):
        parse_experience(text)
        found = experience_from(text)
        if found is None:
            return
        _, low, high = found
        for value in (low, high):
            if value is not None:
                self.assertTrue(0 <= value <= YEAR_LIMIT)
        if low is not None and high is not None:
            self.assertLessEqual(low, high)


class OutputFuzzTest(unittest.TestCase):
    @given(
        st.one_of(
            st.text(),
            st.sampled_from(FORMULA_START).flatmap(lambda c: st.text().map(lambda t: c + t)),
        )
    )
    @example("'=1+2")
    def test_a_cell_never_starts_a_formula_and_reads_back_as_written(self, value):
        stored = neutralise(value)
        self.assertFalse(stored.startswith(FORMULA_START))
        self.assertEqual(restore(stored), value)

    @given(st.text())
    @example('first\u2028second')
    def test_a_log_entry_stays_one_line(self, text):
        self.assertLessEqual(len(log.escape(text).splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
