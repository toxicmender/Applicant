import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.services.provider_health import Acquisition, LeaseFailure, acquire, health
from applicant.services.research_feed import collect_feed
from tests.research_fixtures import NOW, bundle


class AcquisitionLifecycleTest(unittest.TestCase):
    def test_skips_capture_limit_and_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db'
            self.assertEqual(
                collect_feed(path, {'providers': ['upwork']})['outcomes'][0]['status'],
                'import_reference',
            )
            with Acquisition(path, 'indeed', clock=lambda: NOW):
                factory = Mock()
                result = collect_feed(
                    path, {'providers': ['indeed']}, factory=factory, clock=lambda: NOW
                )
                factory.assert_not_called()
                self.assertEqual(result['outcomes'][0]['status'], 'leased')
            job = bundle().observations[0].job
            client = Mock(
                raw=b'',
                source_urls={},
                requisition_metadata={},
                partial_jobs=[],
                capture=Mock(return_value=[job]),
            )
            result = collect_feed(
                path,
                {
                    'providers': ['builtin'],
                    'urls': {'builtin': ['https://builtin.com/job/1', 'https://builtin.com/job/2']},
                    'limit': 1,
                },
                factory=lambda *a, **k: client,
                clock=lambda: NOW,
            )
            self.assertEqual(len(result['job_version_ids']), 1)
            client.capture.assert_called_once()
            client.close.assert_called_once()
            client.capture.side_effect = [[], [job]]
            client.capture.reset_mock()
            result = collect_feed(
                path,
                {
                    'providers': ['builtin'],
                    'urls': {'builtin': ['https://builtin.com/job/1', 'https://builtin.com/job/2']},
                },
                factory=lambda *a, **k: client,
                clock=lambda: NOW,
            )
            self.assertEqual(client.capture.call_count, 2)
            client.search.side_effect = LeaseFailure('renewal failed')
            result = collect_feed(
                path, {'providers': ['indeed']}, factory=lambda *a, **k: client, clock=lambda: NOW
            )
            self.assertEqual(result['outcomes'][0]['status'], 'infrastructure_failure')
            self.assertEqual(health(path, 'indeed')['failures'], 0)
            result = collect_feed(
                path,
                {'providers': ['naukri']},
                factory=Mock(side_effect=OSError('launch failed')),
                clock=lambda: NOW,
            )
            self.assertEqual(result['outcomes'][0]['status'], 'unavailable')

    def test_renewal_and_abandonment_preserve_health(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db'
            stamp = [NOW]
            with Acquisition(path, 'indeed', clock=lambda: stamp[0]) as owner:
                stamp[0] += timedelta(minutes=9)
                owner.tick()
                self.assertEqual(
                    acquire(path, 'indeed', clock=lambda: NOW + timedelta(minutes=11))['status'],
                    'leased',
                )
            self.assertIsNone(health(path, 'indeed')['lease_until'])
            self.assertEqual(health(path, 'indeed')['failures'], 0)

    def test_background_exception_is_relayed(self):
        owner = Acquisition(None, 'indeed')
        with (
            patch.object(owner.stop, 'wait', return_value=False),
            patch.object(owner, 'tick', side_effect=RuntimeError('failed')),
        ):
            owner._run()
        with self.assertRaises(LeaseFailure):
            owner.check()

    def test_validation_precedes_acquisition(self):
        cases = [
            None,
            {},
            {'providers': 'indeed'},
            {'providers': [1]},
            {'providers': ['indeed'], 'keywords': 1},
            {'providers': ['indeed'], 'urls': []},
            {'providers': ['indeed'], 'require_all_sources': 1},
            {'providers': ['indeed', 'indeed']},
            {'providers': ['indeed'], 'limit': 0},
            {'providers': ['indeed'], 'urls': {'indeed': ['https://indeed.com/job']}},
        ]
        with patch('applicant.services.research_feed.Acquisition') as lease:
            for query in cases:
                with self.subTest(query=query), self.assertRaises(ValueError):
                    collect_feed('unused', query)
            lease.assert_not_called()
