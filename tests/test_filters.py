from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from applicant.filters import JobFilter
from applicant.models import Job
from applicant.money import Rates

# every currency test runs off a fixed table, so no test reaches the network and
# a change in real rates can never turn the suite red
OFFLINE = Rates(path='/nonexistent/money-cache.json', offline=True)


def job(**kwargs) -> Job:
    fields: dict[str, Any] = {
        'source': 'indeed',
        'title': 'Python Developer',
        'company': 'Acme',
        'location': 'Bengaluru, India',
    }
    fields.update(kwargs)
    return Job(**fields)


class TextMatchingTest(unittest.TestCase):
    def test_words_match_in_any_order(self):
        keep, _ = JobFilter(title='python senior').matches(job(title='Senior Python Engineer'))
        self.assertTrue(keep)

    def test_every_word_must_appear(self):
        keep, _ = JobFilter(title='senior rust').matches(job(title='Senior Python Engineer'))
        self.assertFalse(keep)

    def test_matching_is_case_insensitive(self):
        keep, _ = JobFilter(company='ACME').matches(job(company='Acme Corp'))
        self.assertTrue(keep)

    def test_a_missing_field_cannot_match(self):
        keep, _ = JobFilter(company='acme').matches(job(company=None))
        self.assertFalse(keep)

    def test_unset_filters_constrain_nothing(self):
        keep, flags = JobFilter().matches(job())
        self.assertTrue(keep)
        self.assertEqual(flags, [])


class SkipTest(unittest.TestCase):
    """The board already filtered these; re-checking locally throws away good results."""

    def test_location_is_not_rechecked_when_skipped(self):
        # a country search answers with bare city names, which would never match 'India'
        posting = job(location='Bengaluru')
        keep, _ = JobFilter(location='India').matches(posting)
        self.assertFalse(keep, 'sanity: a local check does reject this')

        keep, _ = JobFilter(location='India').matches(posting, skip=['location'])
        self.assertTrue(keep, 'the board already applied the location filter')

    def test_posted_is_not_rechecked_when_skipped(self):
        posting = job(posted=None)
        keep, flags = JobFilter(posted_within_days=7, keep_unknown=False).matches(posting)
        self.assertFalse(keep)
        self.assertIn('date-unknown', flags)

        keep, flags = JobFilter(posted_within_days=7, keep_unknown=False).matches(
            posting, skip=['posted']
        )
        self.assertTrue(keep)
        self.assertEqual(flags, [])


class SalaryFilterTest(unittest.TestCase):
    def test_above_the_floor_is_kept_unflagged(self):
        keep, flags = JobFilter(min_salary=1_000_000).matches(job(salary='15 Lacs PA'))
        self.assertTrue(keep)
        self.assertEqual(flags, [])

    def test_below_the_floor_is_dropped(self):
        keep, flags = JobFilter(min_salary=2_000_000).matches(job(salary='5 Lacs PA'))
        self.assertFalse(keep)
        self.assertEqual(flags, [])

    def test_unknown_salary_is_kept_and_flagged_by_default(self):
        keep, flags = JobFilter(min_salary=1_000_000).matches(job(salary=None))
        self.assertTrue(keep)
        self.assertEqual(flags, ['salary-unknown'])

    def test_strict_drops_unknown_salary(self):
        keep, flags = JobFilter(min_salary=1_000_000, keep_unknown=False).matches(
            job(salary='Not disclosed')
        )
        self.assertFalse(keep)
        self.assertEqual(flags, ['salary-unknown'])

    def test_another_currency_is_converted_by_purchasing_power(self):
        """$120k is well past a ₹10L floor once compared at PPP."""
        keep, flags = JobFilter(min_salary=1_000_000, currency='INR', rates=OFFLINE).matches(
            job(salary='$120,000 a year')
        )
        self.assertTrue(keep)
        self.assertEqual(flags, [])

    def test_conversion_can_still_reject(self):
        keep, _ = JobFilter(min_salary=100_000_000, currency='INR', rates=OFFLINE).matches(
            job(salary='$120,000 a year')
        )
        self.assertFalse(keep)

    def test_strict_refuses_to_compare_across_currencies(self):
        keep, flags = JobFilter(
            min_salary=1_000_000, currency='INR', salary_basis='strict', rates=OFFLINE
        ).matches(job(salary='$120,000 a year'))
        self.assertTrue(keep, 'unverifiable, so kept and flagged')
        self.assertEqual(flags, ['salary-currency-mismatch'])

    def test_strict_and_no_keep_unknown_drops_the_mismatch(self):
        keep, flags = JobFilter(
            min_salary=1_000_000,
            currency='INR',
            salary_basis='strict',
            keep_unknown=False,
            rates=OFFLINE,
        ).matches(job(salary='$120,000 a year'))
        self.assertFalse(keep)
        self.assertEqual(flags, ['salary-currency-mismatch'])

    def test_an_unconvertible_currency_is_flagged_not_invented(self):
        # ISK is in neither the PPP seed nor an offline FX table
        keep, flags = JobFilter(min_salary=1_000_000, currency='ISK', rates=OFFLINE).matches(
            job(salary='$120,000 a year')
        )
        self.assertTrue(keep)
        self.assertEqual(flags, ['rate-unavailable'])

    def test_a_bare_number_is_assumed_to_be_in_the_filter_currency(self):
        keep, flags = JobFilter(min_salary=1_000_000, currency='INR', rates=OFFLINE).matches(
            job(salary='1500000 per annum')
        )
        self.assertTrue(keep)
        self.assertEqual(flags, ['salary-currency-assumed'])


class DateFilterTest(unittest.TestCase):
    TODAY = date(2026, 8, 3)

    def test_recent_posting_is_kept(self):
        keep, flags = JobFilter(posted_within_days=7).matches(
            job(posted='2026-07-30'), today=self.TODAY
        )
        self.assertTrue(keep)
        self.assertEqual(flags, [])

    def test_boundary_is_inclusive(self):
        keep, _ = JobFilter(posted_within_days=7).matches(
            job(posted='2026-07-27'), today=self.TODAY
        )
        self.assertTrue(keep)

    def test_old_posting_is_dropped(self):
        keep, _ = JobFilter(posted_within_days=7).matches(
            job(posted='2026-06-01'), today=self.TODAY
        )
        self.assertFalse(keep)

    def test_a_full_timestamp_is_truncated_to_its_date(self):
        keep, _ = JobFilter(posted_within_days=7).matches(
            job(posted='2026-07-30T11:22:33Z'), today=self.TODAY
        )
        self.assertTrue(keep)

    def test_an_unparseable_date_is_flagged(self):
        keep, flags = JobFilter(posted_within_days=7).matches(
            job(posted='last tuesday'), today=self.TODAY
        )
        self.assertTrue(keep)
        self.assertEqual(flags, ['date-unknown'])


class FlagAccumulationTest(unittest.TestCase):
    def test_both_unknowns_are_reported(self):
        keep, flags = JobFilter(min_salary=100, posted_within_days=7).matches(
            job(salary=None, posted=None)
        )
        self.assertTrue(keep)
        self.assertEqual(flags, ['salary-unknown', 'date-unknown'])

    def test_flags_gathered_before_a_rejection_are_still_returned(self):
        keep, flags = JobFilter(min_salary=100, posted_within_days=7, keep_unknown=False).matches(
            job(salary=None, posted=None)
        )
        self.assertFalse(keep)
        self.assertEqual(flags, ['salary-unknown'])


if __name__ == '__main__':
    unittest.main()
