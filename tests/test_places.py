"""Containment, and the third answer.

`within` has to say yes, no, or "cannot tell" - and the last of those is the
whole point: a table of places nobody maintains would quietly drop correct
results, so anything it cannot settle is handed back for the caller to flag.
"""

from __future__ import annotations

import unittest

from applicant.places import COUNTRIES, COUNTRY_OF_PLACE, countries_in, country_for, within


class WithinTest(unittest.TestCase):
    def test_a_city_is_inside_its_country(self):
        for location in (
            'Bengaluru',
            'Bengaluru, Karnataka',
            'Hyderabad, Telangana',
            'Pune, Maharashtra',
            'Mumbai, Maharashtra',
            'New Delhi',
            'Gurugram, Haryana',
        ):
            with self.subTest(location=location):
                self.assertIs(within('India', location), True)

    def test_the_country_spelled_out_still_matches(self):
        self.assertIs(within('India', 'Bengaluru, India'), True)

    def test_case_and_spacing_do_not_matter(self):
        self.assertIs(within('  india  ', 'BENGALURU, KARNATAKA'), True)

    def test_a_renamed_city_matches_under_either_name(self):
        for pair in (('bangalore', 'bengaluru'), ('bombay', 'mumbai'), ('calcutta', 'kolkata')):
            with self.subTest(pair=pair):
                for name in pair:
                    self.assertIs(within('India', name.title()), True)

    def test_somewhere_else_is_not_inside(self):
        for location in ('Dublin, Ireland', 'Berlin, Germany', 'Austin, US', 'Singapore'):
            with self.subTest(location=location):
                self.assertIs(within('India', location), False)

    def test_a_place_we_do_not_know_cannot_be_settled(self):
        for location in ('Remote', 'Anywhere', 'Springfield'):
            with self.subTest(location=location):
                self.assertIsNone(within('India', location))

    def test_a_missing_location_is_not_a_match(self):
        """A posting that says nowhere cannot be shown to be somewhere."""
        self.assertIs(within('India', None), False)
        self.assertIs(within('India', ''), False)

    def test_an_empty_filter_constrains_nothing(self):
        self.assertIs(within('', 'Bengaluru'), True)

    def test_a_city_filter_behaves_as_plain_text(self):
        self.assertIs(within('Bengaluru', 'Bengaluru, Karnataka'), True)
        self.assertIs(within('Bengaluru', 'Hyderabad, Telangana'), False)

    def test_a_city_filter_does_not_reach_for_the_table(self):
        """We know Bengaluru is in Karnataka; we do not model state to city."""
        self.assertIs(within('Karnataka', 'Bengaluru'), False)

    def test_words_may_arrive_in_any_order(self):
        self.assertIs(within('Karnataka Bengaluru', 'Bengaluru, Karnataka'), True)

    def test_a_word_boundary_keeps_india_out_of_indiana(self):
        self.assertIs(within('India', 'Indianapolis, Indiana'), None)


class CountryForTest(unittest.TestCase):
    def test_a_bare_city_resolves_to_its_country(self):
        self.assertEqual(country_for('Bengaluru'), 'india')
        self.assertEqual(country_for('Hyderabad, Telangana'), 'india')

    def test_a_named_country_resolves_to_itself(self):
        self.assertEqual(country_for('Dublin, Ireland'), 'ireland')

    def test_an_unknown_place_resolves_to_nothing(self):
        self.assertIsNone(country_for('Springfield'))
        self.assertIsNone(country_for(''))

    def test_an_ambiguous_location_resolves_to_nothing(self):
        """Two countries named is not one answer, so it is not an answer."""
        self.assertIsNone(country_for('Bengaluru, India and Dublin, Ireland'))


class TableTest(unittest.TestCase):
    def test_every_indian_place_maps_back_to_india(self):
        self.assertEqual(set(COUNTRY_OF_PLACE.values()), {'india'})

    def test_the_places_are_lowercase_so_lookups_land(self):
        for place in COUNTRY_OF_PLACE:
            with self.subTest(place=place):
                self.assertEqual(place, place.lower())

    def test_india_is_among_the_country_names(self):
        self.assertIn('india', COUNTRIES)

    def test_countries_in_reports_both_routes(self):
        self.assertEqual(countries_in('Bengaluru, Karnataka'), {'india'})
        self.assertEqual(countries_in('Dublin, Ireland'), {'ireland'})
        self.assertEqual(countries_in('Nowhere'), set())


if __name__ == '__main__':
    unittest.main()
