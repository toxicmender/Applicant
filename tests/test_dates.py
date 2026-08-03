from __future__ import annotations

import unittest
from datetime import datetime, timezone

from applicant.dates import epoch_to_iso, relative_to_iso

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class RelativeToIsoTest(unittest.TestCase):
    def test_days(self):
        self.assertEqual(relative_to_iso('6 days ago', now=NOW), '2026-07-28')

    def test_singular_unit(self):
        self.assertEqual(relative_to_iso('1 day ago', now=NOW), '2026-08-02')

    def test_weeks(self):
        self.assertEqual(relative_to_iso('2 weeks ago', now=NOW), '2026-07-20')

    def test_months_are_thirty_days(self):
        self.assertEqual(relative_to_iso('1 month ago', now=NOW), '2026-07-04')

    def test_hours_stay_on_the_same_day(self):
        self.assertEqual(relative_to_iso('3 hours ago', now=NOW), '2026-08-03')

    def test_minutes(self):
        self.assertEqual(relative_to_iso('30 minutes ago', now=NOW), '2026-08-03')

    def test_the_plus_suffix_boards_use(self):
        self.assertEqual(relative_to_iso('30+ days ago', now=NOW), '2026-07-04')

    def test_embedded_in_a_longer_string(self):
        self.assertEqual(relative_to_iso('Posted 6 days ago by Acme', now=NOW), '2026-07-28')

    def test_case_insensitive(self):
        self.assertEqual(relative_to_iso('6 Days Ago', now=NOW), '2026-07-28')

    def test_non_relative_text_is_none(self):
        for text in ('Just posted', 'Today', '2026-08-01', '', None):
            with self.subTest(text=text):
                self.assertIsNone(relative_to_iso(text, now=NOW))


class EpochToIsoTest(unittest.TestCase):
    # derived rather than written out, so the constant cannot drift from the date
    MILLIS = int(NOW.timestamp() * 1000)

    def test_milliseconds(self):
        self.assertEqual(epoch_to_iso(self.MILLIS), '2026-08-03')

    def test_float_milliseconds(self):
        self.assertEqual(epoch_to_iso(float(self.MILLIS)), '2026-08-03')

    def test_non_numbers_are_none(self):
        for value in (None, '', 'yesterday', {}, []):
            with self.subTest(value=value):
                self.assertIsNone(epoch_to_iso(value))

    def test_booleans_are_not_timestamps(self):
        self.assertIsNone(epoch_to_iso(True))

    def test_out_of_range_is_none_not_a_crash(self):
        self.assertIsNone(epoch_to_iso(10**20))


if __name__ == '__main__':
    unittest.main()
