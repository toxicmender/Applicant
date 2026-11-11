"""Fixture-based acquisition and durable provider failure handling."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import httpx

from applicant.boards.public import SITES, PublicBoard, structured_records
from applicant.boards.weworkremotely import WeWorkRemotely
from applicant.errors import AuthFailed, Blocked, Unparseable
from applicant.infra.provider_registry import board_client
from applicant.services.company_capture import capture_profile
from applicant.services.provider_health import acquire, finish, health
from applicant.services.research import import_research, read_research
from applicant.services.research_feed import collect_feed
from tests.research_fixtures import NOW, bundle
from tests.research_pages import html, posting


class PublicAcquisitionTest(unittest.TestCase):
    def test_authentication_expiry_preserves_completed_urls(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: (
                        httpx.Response(401, text='login')
                        if request.url.path.endswith('/two')
                        else httpx.Response(
                            200, text=html(posting('https://employer.example/jobs/REQ-42'))
                        )
                    )
                )
            ) as client,
        ):
            path = Path(folder) / 'db.sqlite'

            def factory(name, **kwargs):
                board = PublicBoard(name, client=client)
                board.http.interval = 0
                return board

            result = collect_feed(
                path,
                {
                    'providers': ['wellfound'],
                    'limit': 25,
                    'urls': {
                        'wellfound': [
                            'https://wellfound.com/jobs/one',
                            'https://wellfound.com/jobs/two',
                        ]
                    },
                },
                factory=factory,
                clock=lambda: NOW,
            )
            self.assertEqual(len(result['job_version_ids']), 1)
            self.assertEqual(result['outcomes'][0]['status'], 'needs_setup')
            self.assertEqual(result['outcomes'][0]['preserved_count'], 1)

    def test_each_public_adapter_and_explicit_requisition_provenance(self):
        for name, (landing, _) in SITES.items():
            with self.subTest(provider=name):
                transport = httpx.MockTransport(
                    lambda request: httpx.Response(
                        200, text=html(posting('https://employer.example/jobs/REQ-42'))
                    )
                )
                with httpx.Client(transport=transport) as client:
                    board = PublicBoard(name, client=client)
                    board.http.interval = 0
                    jobs = board.search('Python', location='Bengaluru')
                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(jobs[0].id, 'REQ-42')
                    self.assertIn('100000', jobs[0].salary or '')
                    self.assertEqual(
                        board.requisition_metadata['REQ-42']['employer_requisition_url'],
                        'https://employer.example/jobs/REQ-42',
                    )
                    self.assertEqual(board.source_urls['REQ-42'], landing)
                    board.close()
        records, _ = structured_records(
            html({'@graph': [posting('https://example.com/job')]}), 'JobPosting'
        )
        self.assertEqual(len(records), 1)

    def test_block_login_invalid_and_missing_structured_data(self):
        for status, body, error in [
            (403, 'denied', Blocked),
            (401, 'login', AuthFailed),
            (200, '<html>empty</html>', Unparseable),
        ]:
            with (
                self.subTest(status=status),
                httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request, status=status, body=body: httpx.Response(status, text=body)
                    )
                ) as client,
            ):
                board = PublicBoard('wellfound', client=client)
                board.http.interval = 0
                with self.assertRaises(error):
                    board.capture('https://wellfound.com/jobs/123')
                with self.assertRaises(ValueError):
                    board.capture('https://evil.example/jobs/123')
        with self.assertRaises(ValueError):
            board_client('upwork')

    def test_rss_security_attribution_and_unknown_remote_scope(self):
        rss = b'<rss><channel><item><title>Employer: Python Engineer</title><link>https://weworkremotely.com/remote-jobs/employer-python</link><pubDate>Mon, 02 Nov 2026 00:00:00 GMT</pubDate><description>Remote job</description></item></channel></rss>'
        jobs = WeWorkRemotely.parse(rss, 'Python')
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0].location)
        for body in (b'<html/>', b'bad XML', b'<!DOCTYPE rss><rss/>'):
            with self.assertRaises(Unparseable):
                WeWorkRemotely.parse(body)
        with httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, content=rss))
        ) as client:
            board = WeWorkRemotely(client=client)
            board.http.interval = 0
            self.assertEqual(len(board.search('Python')), 1)
            board.close()

    def test_live_collection_path_crosschecks_two_mocked_public_boards(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            clients = []

            def factory(name, **kwargs):
                client = httpx.Client(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(
                            200, text=html(posting('https://employer.example/jobs/REQ-42'))
                        )
                    )
                )
                clients.append(client)
                result = PublicBoard(name, client=client)
                result.http.interval = 0
                return result

            try:
                result = collect_feed(
                    path,
                    {'providers': ['wellfound', 'builtin'], 'limit': 1},
                    factory=factory,
                    clock=lambda: NOW,
                )
                self.assertEqual(result['groups'][0]['status'], 'confirmed')
                self.assertEqual(len(result['groups'][0]['sources']), 2)
                self.assertEqual(
                    result['observations'][0]['content_representation'], 'http_response'
                )
                self.assertEqual(health(path, 'builtin')['status'], 'ready')
            finally:
                for client in clients:
                    client.close()

    def test_cooldown_preserves_partial_collection_and_skips_future_acquisition(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            good = Mock(
                search=Mock(return_value=[bundle().observations[0].job]),
                close=Mock(),
                raw=b'',
                source_urls={},
                requisition_metadata={},
            )
            bad = Mock(search=Mock(side_effect=Blocked('CAPTCHA')), close=Mock())

            def factory(name, **kwargs):
                return good if name == 'linkedin' else bad

            result = collect_feed(
                path, {'providers': ['linkedin', 'indeed']}, factory=factory, clock=lambda: NOW
            )
            self.assertEqual(len(result['job_version_ids']), 1)
            self.assertEqual(health(path, 'indeed')['status'], 'cooldown')
            self.assertEqual(acquire(path, 'indeed', clock=lambda: NOW)['status'], 'cooldown')


class HealthTest(unittest.TestCase):
    def test_leases_counters_cooldowns_and_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            for _index in range(3):
                lease = acquire(path, 'indeed', clock=lambda: NOW)
                self.assertEqual(acquire(path, 'indeed', clock=lambda: NOW)['status'], 'leased')
                result = finish(path, 'indeed', lease['token'], 'unavailable', clock=lambda: NOW)
            self.assertEqual(result['status'], 'cooldown')
            later = NOW + timedelta(minutes=16)
            lease = acquire(path, 'indeed', clock=lambda: later)
            self.assertEqual(lease['status'], 'acquired')
            result = finish(path, 'indeed', lease['token'], 'success', clock=lambda: later)
            self.assertEqual(result['failures'], 0)
            with self.assertRaises(ValueError):
                finish(path, 'indeed', 'wrong-token', 'success')

    def test_verified_profile_capture_keeps_raw_and_assertion_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite'
            data = bundle().model_dump(mode='json')
            locator = 'https://flexa.careers/companies/startup'
            data['identifiers'].append(
                {
                    'identifier_id': 'flexa-startup',
                    'entity_id': 'startup',
                    'provider': 'flexa',
                    'provider_company_id': 'startup',
                    'verified_at': NOW.isoformat(),
                    'verification_url': locator,
                }
            )
            import_research(path, data)
            record = {
                '@type': 'Organization',
                'url': locator,
                'name': 'Startup',
                'description': 'Employer-provided profile description',
            }
            with httpx.Client(
                transport=httpx.MockTransport(lambda req: httpx.Response(200, text=html(record)))
            ) as client:
                result = capture_profile(
                    path,
                    {
                        'entity_id': 'startup',
                        'provider': 'flexa',
                        'provider_company_id': 'startup',
                        'url': locator,
                    },
                    client=client,
                    clock=lambda: NOW,
                )
            evidence = next(
                x for x in read_research(path)['evidence'] if x.evidence_id == result['evidence_id']
            )
            self.assertEqual(evidence.assertion, 'employer')
            self.assertIsNone(evidence.observed_at)
            self.assertTrue(evidence.responses)
