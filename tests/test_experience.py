"""Required experience: parsing the year range, and matching yours against it.

Only Naukri publishes experience as its own field, so most postings carry none
and the range is derived from whatever text there is.
"""

from __future__ import annotations

import unittest

from applicant.filters import JobFilter
from applicant.models import Job, parse_experience


class ParseExperienceTest(unittest.TestCase):
    def test_a_range(self):
        self.assertEqual(parse_experience('0-2 Yrs'), (0.0, 2.0))
        self.assertEqual(parse_experience('3 - 7 years'), (3.0, 7.0))

    def test_a_range_written_with_to(self):
        self.assertEqual(parse_experience('2 to 5 years'), (2.0, 5.0))

    def test_an_en_dash_range(self):
        self.assertEqual(parse_experience('2–5 yrs'), (2.0, 5.0))

    def test_an_open_ended_minimum_has_no_ceiling(self):
        """'5+ years' must not collapse to '5 to 5' - it means five or more."""
        self.assertEqual(parse_experience('5+ years'), (5.0, None))
        self.assertEqual(parse_experience('5+ yrs'), (5.0, None))

    def test_minimum_phrasing(self):
        self.assertEqual(parse_experience('minimum 4 years'), (4.0, None))
        self.assertEqual(parse_experience('at least 6 yrs'), (6.0, None))

    def test_an_exact_figure(self):
        self.assertEqual(parse_experience('3 years'), (3.0, 3.0))

    def test_fractional_years(self):
        self.assertEqual(parse_experience('1.5-3 yrs'), (1.5, 3.0))

    def test_fresher_wording_means_zero(self):
        for text in ('Fresher', 'Entry-level role', 'no experience', 'Graduate Trainee'):
            with self.subTest(text=text):
                self.assertEqual(parse_experience(text), (0.0, 0.0))

    def test_no_experience_stated(self):
        for text in ('', None, 'Python Developer', 'Full-time'):
            with self.subTest(text=text):
                self.assertEqual(parse_experience(text), (None, None))

    def test_embedded_in_a_longer_string(self):
        self.assertEqual(parse_experience('Looking for 4-8 Yrs in backend'), (4.0, 8.0))


class JobExperienceTest(unittest.TestCase):
    def test_the_range_is_derived_from_the_board_text(self):
        job = Job(source='naukri', title='Dev', experience_text='2-5 Yrs')
        self.assertEqual((job.experience_min, job.experience_max), (2.0, 5.0))

    def test_the_title_is_used_when_there_is_no_experience_field(self):
        job = Job(source='indeed', title='Senior Engineer (5+ years)')
        self.assertEqual((job.experience_min, job.experience_max), (5.0, None))

    def test_an_explicit_range_is_not_overwritten(self):
        job = Job(
            source='naukri',
            title='Dev',
            experience_text='2-5 Yrs',
            experience_min=1.0,
            experience_max=9.0,
        )
        self.assertEqual((job.experience_min, job.experience_max), (1.0, 9.0))

    def test_an_inverted_range_is_reordered(self):
        job = Job(source='naukri', title='Dev', experience_min=8.0, experience_max=2.0)
        self.assertEqual((job.experience_min, job.experience_max), (2.0, 8.0))

    def test_absurd_year_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            Job(source='naukri', title='Dev', experience_min=99.0)

    def test_an_absurd_derived_range_reads_as_unknown_not_a_crash(self):
        """A typo in a posting must not fail the scrape that found it."""
        job = Job(source='naukri', title='Dev', experience_text='100 years')
        self.assertEqual((job.experience_min, job.experience_max), (None, None))

    def test_a_derived_range_is_held_to_the_same_bounds_as_an_explicit_one(self):
        ok = Job(source='naukri', title='Dev', experience_text='60 years')
        self.assertEqual(ok.experience_min, 60.0)


class ExperienceFilterTest(unittest.TestCase):
    def job(self, **kwargs) -> Job:
        return Job(source='naukri', title='Developer', **kwargs)

    def test_inside_the_range_qualifies(self):
        keep, flags = JobFilter(experience=3).matches(self.job(experience_text='2-5 Yrs'))
        self.assertTrue(keep)
        self.assertEqual(flags, [])

    def test_under_the_floor_does_not(self):
        keep, _ = JobFilter(experience=1).matches(self.job(experience_text='2-5 Yrs'))
        self.assertFalse(keep)

    def test_over_the_ceiling_does_not(self):
        """A job wanting 0-2 years is not a match for someone with eight."""
        keep, _ = JobFilter(experience=8).matches(self.job(experience_text='0-2 Yrs'))
        self.assertFalse(keep)

    def test_both_bounds_are_inclusive(self):
        for years in (2, 5):
            with self.subTest(years=years):
                keep, _ = JobFilter(experience=years).matches(self.job(experience_text='2-5 Yrs'))
                self.assertTrue(keep)

    def test_an_open_ended_minimum_has_no_upper_bound(self):
        keep, _ = JobFilter(experience=20).matches(self.job(experience_text='5+ years'))
        self.assertTrue(keep)

    def test_unknown_experience_is_kept_and_flagged_by_default(self):
        keep, flags = JobFilter(experience=5).matches(self.job())
        self.assertTrue(keep)
        self.assertEqual(flags, ['experience-unknown'])

    def test_strict_drops_unknown_experience(self):
        keep, flags = JobFilter(experience=5, keep_unknown=False).matches(self.job())
        self.assertFalse(keep)
        self.assertEqual(flags, ['experience-unknown'])

    def test_the_check_can_be_skipped(self):
        keep, flags = JobFilter(experience=5, keep_unknown=False).matches(
            self.job(), skip=['experience']
        )
        self.assertTrue(keep)
        self.assertEqual(flags, [])

    def test_an_unset_filter_constrains_nothing(self):
        keep, flags = JobFilter().matches(self.job(experience_text='0-2 Yrs'))
        self.assertTrue(keep)
        self.assertEqual(flags, [])


if __name__ == '__main__':
    unittest.main()
