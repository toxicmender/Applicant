import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

import httpx
from tools.live_requisition import ListingCapture, candidate, release_eligible, verify

from applicant.domain.research_identity import match_jobs
from applicant.errors import Unparseable
from tests.research_fixtures import NOW, bundle

URLS = {
    'builtin': 'https://builtin.com/job/one',
    'linkedin': 'https://www.linkedin.com/jobs/view/2',
}
SHA = 'a' * 40


class LiveRequisitionGateTest(unittest.TestCase):
    def test_capture_rejects_a_listing_redirect_to_another_host(self):
        board = ListingCapture('builtin', URLS, None)
        self.addCleanup(board.close)
        response = httpx.Response(200, request=httpx.Request('GET', 'https://example.com/job/42'))
        with (
            patch.object(board.http, 'get', return_value=response),
            self.assertRaisesRegex(Unparseable, 'outside the selected board'),
        ):
            board.search('')

    def test_promotion_requires_fresh_verification_for_release_sha(self):
        report = {
            'kind': 'live_requisition_validation',
            'schema_version': 1,
            'status': 'verified',
            'match_revision': 'match',
            'tested_sha': SHA,
            'checked_at': NOW.isoformat(),
        }
        self.assertTrue(release_eligible(report, SHA, clock=lambda: NOW + timedelta(hours=24)))
        self.assertFalse(release_eligible(report, SHA, clock=lambda: NOW + timedelta(hours=25)))
        self.assertFalse(release_eligible(report, SHA, clock=lambda: NOW - timedelta(seconds=1)))
        self.assertFalse(release_eligible(report, 'b' * 40, clock=lambda: NOW))
        self.assertFalse(release_eligible({**report, 'status': 'blocked'}, SHA, clock=lambda: NOW))

    def test_preflight_rejects_aliases_unsafe_hosts_and_queries(self):
        for invalid in (
            {
                'listings': {
                    'otta': 'https://www.welcometothejungle.com/jobs/one',
                    'linkedin': URLS['linkedin'],
                }
            },
            {'listings': {**URLS, 'builtin': 'https://builtin.com.example/job/one'}},
            {'listings': {**URLS, 'builtin': URLS['builtin'] + '?session=secret'}},
            {'listings': {**URLS, 'builtin': 'https://user@builtin.com/job/one'}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                candidate(invalid)

    def test_verified_failed_and_blocked(self):
        base = bundle().observations[0]
        for status in ('verified', 'failed', 'blocked'):
            with self.subTest(status=status):
                observations = [
                    base.model_copy(
                        update={
                            'job': base.job.model_copy(update={'source': name}),
                            'job_version_id': name,
                            'employer_requisition_url': 'https://example.com/requisition/42'
                            if status != 'failed'
                            else None,
                            'employer_entity_id': None,
                            'employer_requisition_id': None,
                            'origin': 'agency' if name == 'builtin' else 'direct',
                        }
                    )
                    for name in URLS
                ]
                collector = Mock(
                    return_value={
                        'outcomes': [
                            {
                                'provider': name,
                                'status': 'blocked' if status == 'blocked' else 'success',
                            }
                            for name in URLS
                        ],
                        'observations': [x.model_dump(mode='json') for x in observations],
                        'groups': match_jobs(observations),
                    }
                )
                report = verify({'listings': URLS}, SHA, collector=collector)
                self.assertEqual(report['status'], status)
                self.assertEqual(report['tested_sha'], SHA)
                self.assertFalse(report['independent_corroboration'])
                self.assertNotIn('builtin', report['direct_sources'])
                self.assertNotIn('job', report['observations'][0])
