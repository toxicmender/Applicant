"""A failed login attempt leaves the installed credential usable."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.services.provider_setup import session_path, session_status, setup_provider
from applicant.settings import Settings
from tests.test_setup_lifecycle import STATE


class SessionRecoveryContractTest(unittest.TestCase):
    def test_cancelled_reauthentication_preserves_installed_state_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as folder, patch('sys.stdin.isatty', return_value=True):
            settings = Settings(data_dir=Path(folder), store='files')
            verify = Mock(return_value=STATE)
            setup_provider(settings, 'linkedin', verifier=verify)
            target = session_path(settings, 'linkedin')
            before = target.read_bytes()
            with self.assertRaises(KeyboardInterrupt):
                setup_provider(
                    settings, 'linkedin', reauth=True, verifier=Mock(side_effect=KeyboardInterrupt)
                )
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'verified')
            self.assertEqual(
                setup_provider(settings, 'linkedin', reauth=True, verifier=verify)['status'],
                'verified',
            )
