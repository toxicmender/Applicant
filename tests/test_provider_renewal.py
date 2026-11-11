import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.errors import DatabaseBusy
from applicant.services.provider_health import (
    Acquisition,
    LeaseFailure,
    abandon,
    acquire,
    health,
    renew,
)
from tests.research_fixtures import NOW


class ProviderRenewalTest(unittest.TestCase):
    def test_failed_abandonment_leaves_expiry_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db'
            with (
                patch(
                    'applicant.services.provider_health.abandon', side_effect=DatabaseBusy('busy')
                ),
                self.assertRaisesRegex(LeaseFailure, 'retry after expiry'),
                Acquisition(path, 'indeed', clock=lambda: NOW),
            ):
                pass
            self.assertEqual(health(path, 'indeed')['failures'], 0)
            self.assertEqual(acquire(path, 'indeed', clock=lambda: NOW)['status'], 'leased')
            after = NOW + timedelta(minutes=10)
            self.assertEqual(acquire(path, 'indeed', clock=lambda: after)['status'], 'acquired')

    def test_renewal_busy_and_shutdown_without_sleeping(self):
        owner = Acquisition(None, 'indeed')
        with (
            patch.object(owner.stop, 'wait', return_value=False) as wait,
            patch(
                'applicant.services.provider_health.renew', side_effect=[DatabaseBusy('busy'), None]
            ) as renewal,
        ):
            owner.tick()
            wait.assert_called_once_with(1)
            self.assertEqual(renewal.call_count, 2)
        for cancelled in (False, True):
            with (
                patch.object(owner.stop, 'wait', return_value=cancelled),
                patch(
                    'applicant.services.provider_health.renew', side_effect=DatabaseBusy('busy')
                ) as renewal,
            ):
                if cancelled:
                    owner.tick()
                else:
                    with self.assertRaises(DatabaseBusy):
                        owner.tick()
                self.assertEqual(renewal.call_count, 1 if cancelled else 2)
        thread = owner.thread = Mock()
        with self.assertRaisesRegex(LeaseFailure, '12 seconds'):
            owner.shutdown()
        thread.join.assert_called_once_with(12)

    def test_start_failure_releases_and_stale_token_cannot_touch_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db'
            with patch('applicant.services.provider_health.Thread') as thread:
                thread.return_value.start.side_effect = RuntimeError('startup')
                with self.assertRaisesRegex(RuntimeError, 'startup'), Acquisition(path, 'indeed'):
                    self.fail('unexpected lease')
            self.assertIsNone(health(path, 'indeed')['lease_until'])
            first = acquire(path, 'indeed', clock=lambda: NOW)['token']
            later = NOW + timedelta(minutes=11)
            second = acquire(path, 'indeed', clock=lambda: later)['token']
            with self.assertRaisesRegex(ValueError, 'ownership'):
                renew(path, 'indeed', first)
            abandon(path, 'indeed', first)
            self.assertEqual(acquire(path, 'indeed', clock=lambda: later)['status'], 'leased')
            abandon(path, 'indeed', second)
            self.assertIsNone(health(path, 'indeed')['lease_until'])
