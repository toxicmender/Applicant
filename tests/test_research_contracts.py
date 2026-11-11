"""Import boundaries reject contradictory identity and malformed evidence."""

import unittest

from hypothesis import given
from hypothesis import strategies as st

from applicant.domain.canonical import canonical_json, content_hash
from applicant.domain.research import ResearchImport
from tests.research_fixtures import bundle


class ResearchContractsTest(unittest.TestCase):
    @given(st.integers(min_value=0, max_value=500000))
    def test_canonical_revalidation_preserves_import_identity(self, minimum):
        data = bundle().model_dump(mode='json')
        fact = data['evidence'][1]['payload']
        fact.update(minimum=str(minimum), maximum=str(minimum + 100))
        parsed = ResearchImport.model_validate(data)
        self.assertEqual(
            content_hash(parsed),
            content_hash(ResearchImport.model_validate_json(canonical_json(parsed))),
        )

    def test_invalid_fact_and_identity_boundaries(self):
        for update in (
            lambda data: data.update(unknown=[]),
            lambda data: data['events'][0].update(related_id='startup'),
            lambda data: data['events'][0].update(supersedes='acquisition'),
            lambda data: data['identifiers'][0].update(
                effective_from='2026-11-01', effective_to='2025-01-01'
            ),
            lambda data: data['evidence'][0].update(job_version_id=None),
            lambda data: data['evidence'][1]['payload'].update(minimum='999999', maximum='1'),
            lambda data: data['evidence'][1]['payload'].update(currency='XXX1'),
            lambda data: data['observations'][0].update(observed_at='2027-01-01T00:00:00Z'),
        ):
            data = bundle().model_dump(mode='json')
            update(data)
            with self.subTest(update=update), self.assertRaises(ValueError):
                ResearchImport.model_validate(data)
