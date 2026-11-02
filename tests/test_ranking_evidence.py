import unittest
from typing import Any

from applicant.domain.ranking_facts import RankingFacts
from applicant.reviews.models import CompanyRating
from applicant.services.ranking_evidence import capture_rating
from tests.ranking_fixtures import NOW


class RatingCaptureTest(unittest.TestCase):
    def test_capture_keeps_raw_payload_and_does_not_reinterpret_overall_rating(self):
        rating = CompanyRating(
            source='glassdoor',
            company='Acme',
            url='https://example.com',
            company_id='acme',
            overall_rating=5,
            review_count=10,
            rating_breakdown={'culture_and_values': 4.3, 'overall': 5},
        )
        typed, raw = capture_rating(rating, verified_company_id='acme', observed_at=NOW)
        facts = RankingFacts.model_validate(typed.payload)
        self.assertEqual(len(facts.ratings), 1)
        self.assertEqual(str(facts.ratings[0].value), '4.3')
        self.assertEqual(raw.payload['overall_rating'], 5)
        rating.rating_breakdown['culture_and_values'] = 1
        self.assertEqual(raw.payload['rating_breakdown']['culture_and_values'], 4.3)
        self.assertEqual(str(facts.ratings[0].value), '4.3')

    def test_company_identity_counts_and_invalid_categories(self):
        base: dict[str, Any] = {
            'source': 'ambitionbox',
            'company': 'Acme',
            'url': 'https://example.com',
            'company_id': 'acme',
        }
        for change in (
            {'company_id': 'wrong'},
            {'source': 'unknown'},
            {'review_count': -1},
            {'review_count': True},
            {'rating_breakdown': {'job_security': True}},
            {'rating_breakdown': {'job_security': '4.3'}},
            {'rating_breakdown': {'job_security': float('nan')}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                payload: dict[str, Any] = base | change
                capture_rating(
                    CompanyRating(**payload), verified_company_id='acme', observed_at=NOW
                )
        typed, _ = capture_rating(
            CompanyRating(**base, review_count=0, rating_breakdown={'job_security': 5}),
            verified_company_id='acme',
            observed_at=NOW,
        )
        self.assertEqual(RankingFacts.model_validate(typed.payload).ratings, ())
