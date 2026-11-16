import unittest
from datetime import timedelta
from unittest.mock import patch

from applicant.domain.research import CompanyIdentifier, CompanyName
from applicant.services.company_capture import capture_profile
from tests.research_fixtures import NOW


class ManualCompanyResolutionTest(unittest.TestCase):
    def test_name_and_country_only_suggest_recorded_legal_entities(self):
        recorded = NOW - timedelta(days=1)
        companies = [
            ('matching', 'GB', 'legal', recorded),
            ('wrong-country', 'US', 'legal', recorded),
            ('trading-name', 'GB', 'trading', recorded),
            ('future', 'GB', 'legal', NOW + timedelta(days=1)),
        ]
        names = [
            CompanyName.model_validate(
                {
                    'name_id': entity,
                    'entity_id': entity,
                    'name': 'Café & Sons, Ltd.',
                    'name_type': name_type,
                    'recorded_at': at,
                    'source_url': 'https://example.org',
                }
            )
            for entity, _, name_type, at in companies
        ]
        identifiers = [
            CompanyIdentifier.model_validate(
                {
                    'identifier_id': entity,
                    'entity_id': entity,
                    'provider': 'legal-country',
                    'provider_company_id': country,
                    'verified_at': recorded,
                    'verification_url': 'https://example.org',
                }
            )
            for entity, country, _, _ in companies
        ]
        with (
            patch(
                'applicant.services.company_capture.read_research',
                return_value={'names': names, 'identifiers': identifiers},
            ),
            patch('applicant.services.company_capture.Acquisition') as acquisition,
        ):
            with self.assertRaisesRegex(ValueError, 'candidates for manual review: matching$'):
                capture_profile(
                    'unused.db',
                    {
                        'entity_id': 'matching',
                        'provider': 'flexa',
                        'provider_company_id': 'cafe',
                        'url': 'https://flexa.careers/companies/cafe',
                        'legal_name': 'CAFÉ and Sons Ltd',
                        'country': 'gb',
                    },
                    clock=lambda: NOW,
                )
            acquisition.assert_not_called()
