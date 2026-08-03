from __future__ import annotations

import json
import unittest

import httpx

from applicant.reviews import AmbitionBoxClient, CompanyNotFound, ParseError, ReviewsError
from applicant.reviews.ambitionbox import slugify
from applicant.reviews.glassdoor import GlassdoorClient, reviews_url
from applicant.reviews.models import CompanyRating, Review

PAGE_PROPS = {
    'companyName': 'TCS',
    'companyId': 4,
    'reviewCount': 117_600,
    'ratingsData': {
        'overallCompanyRating': 3.3,
        'workLifeRating': 3.5,
        'skillDevelopmentRating': 3.6,
    },
    'ratingDistribution': [{'rating': 5, 'count': 37_666}, {'rating': 4, 'count': 40_000}],
    'reviewsData': [
        {
            'overallCompanyRating': 4.0,
            'reviewTitle': 'Good place to start',
            'likesText': 'Job security',
            'disLikesText': 'Slow appraisals',
            'modifiedMachineReadable': '2026-07-01',
            'jobProfile': {'name': 'Systems Engineer'},
            'jobLocation': {'name': 'Pune'},
        }
    ],
}


def next_data_html(props: dict, build_id: str = 'abc123') -> str:
    payload = {'buildId': build_id, 'props': {'pageProps': props}}
    return (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + '</script></head></html>'
    )


def ld_json_html(rating: float, count: int) -> str:
    block = {
        '@type': 'EmployerAggregateRating',
        'ratingValue': rating,
        'ratingCount': count,
        'itemReviewed': {'name': 'TCS'},
    }
    return '<html><script type="application/ld+json">' + json.dumps(block) + '</script></html>'


class SlugifyTest(unittest.TestCase):
    def test_spaces_become_hyphens(self):
        self.assertEqual(slugify('Tata Consultancy'), 'tata-consultancy')

    def test_already_slugged_passes_through(self):
        self.assertEqual(slugify('tcs'), 'tcs')

    def test_a_pasted_reviews_suffix_is_dropped(self):
        """The url template adds -reviews; keeping the caller's would double it."""
        self.assertEqual(slugify('tcs-reviews'), 'tcs')

    def test_punctuation_and_case(self):
        self.assertEqual(slugify('  A.B.C. & Sons!  '), 'a-b-c-sons')


class AmbitionBoxParseTest(unittest.TestCase):
    def setUp(self):
        self.client = AmbitionBoxClient(delay=0)

    def test_page_props_carry_the_build_id(self):
        props = self.client._page_props(next_data_html(PAGE_PROPS, build_id='xyz'))
        self.assertEqual(props['__buildId'], 'xyz')
        self.assertEqual(props['companyName'], 'TCS')

    def test_a_missing_blob_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            self.client._page_props('<html>nothing here</html>')

    def test_a_malformed_blob_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            self.client._page_props('<script id="__NEXT_DATA__">{not json}</script>')

    def test_missing_page_props_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            self.client._page_props('<script id="__NEXT_DATA__">{"props": {}}</script>')

    def test_rating_is_built_from_the_props(self):
        rating = self.client._to_rating(PAGE_PROPS, 'tcs', 'https://x/tcs-reviews')
        self.assertEqual(rating.source, 'ambitionbox')
        self.assertEqual(rating.overall_rating, 3.3)
        self.assertEqual(rating.review_count, 117_600)
        self.assertEqual(rating.company_id, '4')

    def test_only_known_breakdown_keys_are_exposed(self):
        rating = self.client._to_rating(PAGE_PROPS, 'tcs', 'url')
        self.assertEqual(
            rating.rating_breakdown, {'work_life_balance': 3.5, 'skill_development': 3.6}
        )

    def test_the_distribution_is_keyed_by_star(self):
        rating = self.client._to_rating(PAGE_PROPS, 'tcs', 'url')
        self.assertEqual(rating.rating_distribution, {5: 37_666, 4: 40_000})

    def test_review_mapping(self):
        review = self.client._to_review(PAGE_PROPS['reviewsData'][0])
        self.assertEqual(review.rating, 4.0)
        self.assertEqual(review.pros, 'Job security')
        self.assertEqual(review.cons, 'Slow appraisals')
        self.assertEqual(review.job_title, 'Systems Engineer')
        self.assertEqual(review.location, 'Pune')

    def test_json_ld_fallback_still_gives_rating_and_count(self):
        rating = self.client._from_json_ld(ld_json_html(3.3, 117_600), 'tcs', 'url')
        self.assertEqual(rating.overall_rating, 3.3)
        self.assertEqual(rating.review_count, 117_600)

    def test_neither_blob_present_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            self.client._from_json_ld('<html></html>', 'tcs', 'url')


class AmbitionBoxFetchTest(unittest.TestCase):
    def client(self, handler) -> AmbitionBoxClient:
        return AmbitionBoxClient(
            delay=0, client=httpx.Client(transport=httpx.MockTransport(handler))
        )

    def test_a_404_is_a_missing_company(self):
        def handler(request):
            return httpx.Response(404)

        with self.assertRaises(CompanyNotFound):
            self.client(handler).fetch('nope')

    def test_the_first_page_is_enough_for_a_small_request(self):
        def handler(request):
            return httpx.Response(200, text=next_data_html(PAGE_PROPS))

        rating = self.client(handler).fetch('tcs', max_reviews=1)
        self.assertEqual(rating.overall_rating, 3.3)
        self.assertEqual(len(rating.reviews), 1)

    def test_falls_back_to_json_ld_when_next_data_is_gone(self):
        def handler(request):
            return httpx.Response(200, text=ld_json_html(3.3, 117_600))

        rating = self.client(handler).fetch('tcs', max_reviews=5)
        self.assertEqual(rating.overall_rating, 3.3)
        self.assertEqual(rating.reviews, [])

    def test_max_reviews_zero_asks_for_nothing(self):
        def handler(request):
            return httpx.Response(200, text=next_data_html(PAGE_PROPS))

        self.assertEqual(self.client(handler).fetch('tcs', max_reviews=0).reviews, [])

    def test_retries_a_transient_503(self):
        attempts = []

        def handler(request):
            attempts.append(request.url)
            if len(attempts) == 1:
                return httpx.Response(503)
            return httpx.Response(200, text=next_data_html(PAGE_PROPS))

        rating = self.client(handler).fetch('tcs', max_reviews=1)
        self.assertEqual(rating.overall_rating, 3.3)
        self.assertEqual(len(attempts), 2)


class GlassdoorUrlTest(unittest.TestCase):
    def test_slug_with_employer_id(self):
        url, slug, employer = reviews_url('Google-E9079')
        self.assertEqual(url, 'https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm')
        self.assertEqual(slug, 'Google')
        self.assertEqual(employer, '9079')

    def test_a_full_url_is_accepted(self):
        _, slug, employer = reviews_url(
            'https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm'
        )
        self.assertEqual(slug, 'Google')
        self.assertEqual(employer, '9079')

    def test_a_regional_domain_is_accepted(self):
        employer = reviews_url('https://www.glassdoor.co.in/Reviews/Google-Reviews-E9079.htm')[2]
        self.assertEqual(employer, '9079')

    def test_paging_rewrites_the_suffix(self):
        url, _, _ = reviews_url('Google-E9079', page=3)
        self.assertTrue(url.endswith('-E9079_P3.htm'))

    def test_a_bare_name_cannot_be_resolved(self):
        """Glassdoor's company search is behind the same bot check, so we refuse."""
        with self.assertRaises(ReviewsError):
            reviews_url('Google')


class GlassdoorExtractTest(unittest.TestCase):
    def setUp(self):
        self.client = GlassdoorClient()

    def test_reviews_are_found_wherever_they_sit_in_the_payload(self):
        """Apollo cache keys move between deploys, so extraction walks the tree."""
        payload = {
            'ROOT_QUERY': {
                'employerReviews': {
                    'reviews': [
                        {
                            'pros': 'Great pay',
                            'cons': 'Long hours',
                            'summary': 'Mixed',
                            'ratingOverall': 4,
                            'reviewDateTime': '2026-07-01T10:00:00',
                            'jobTitle': {'text': 'SWE'},
                            'location': {'name': 'London'},
                        },
                    ]
                }
            }
        }
        rating = self.client._build([payload], [], 'Google', '9079', 'url')

        self.assertEqual(len(rating.reviews), 1)
        review = rating.reviews[0]
        self.assertEqual(review.pros, 'Great pay')
        self.assertEqual(review.job_title, 'SWE')
        self.assertEqual(review.location, 'London')
        self.assertEqual(review.date, '2026-07-01')

    def test_the_same_review_from_two_payloads_is_recorded_once(self):
        node = {'pros': 'Pay', 'cons': 'Hours', 'summary': 'Fine'}
        rating = self.client._build([{'a': node}, {'b': node}], [], 'Google', '9079', 'url')
        self.assertEqual(len(rating.reviews), 1)

    def test_aggregate_fields_are_picked_up(self):
        payload = {
            'employer': {'ratingOverall': 4.4, 'reviewCount': 12_345, 'ratingWorkLifeBalance': 4.1},
            'r': {'pros': 'p', 'cons': 'c'},
        }
        rating = self.client._build([payload], [], 'Google', '9079', 'url')

        self.assertEqual(rating.overall_rating, 4.4)
        self.assertEqual(rating.review_count, 12_345)
        self.assertEqual(rating.rating_breakdown['work_life_balance'], 4.1)

    def test_a_node_without_pros_or_cons_is_not_a_review(self):
        self.assertIsNone(self.client._read_review({'summary': 'no body'}))

    def test_no_data_at_all_is_a_parse_error(self):
        with self.assertRaises(ParseError):
            self.client._build([], [], 'Google', '9079', 'url')


class ModelTest(unittest.TestCase):
    def test_company_rating_serialises_its_reviews(self):
        rating = CompanyRating(
            source='ambitionbox', company='TCS', url='url', overall_rating=3.3, review_count=10
        )
        rating.reviews.append(Review(rating=4.0, pros='p', cons='c'))

        payload = rating.to_dict()
        self.assertEqual(payload['overall_rating'], 3.3)
        self.assertEqual(payload['reviews'][0]['pros'], 'p')

    def test_defaults_are_not_shared_between_instances(self):
        one = CompanyRating(source='a', company='x', url='u')
        one.reviews.append(Review())
        self.assertEqual(CompanyRating(source='a', company='y', url='u').reviews, [])


if __name__ == '__main__':
    unittest.main()
