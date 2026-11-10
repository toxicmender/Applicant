"""Session state validation and atomic replacement contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.errors import ConfigError
from applicant.services.provider_setup import (
    import_session,
    session_path,
    session_status,
    setup_provider,
    validate_state,
    verify_state,
)
from applicant.settings import Settings

STATE = {
    'cookies': [],
    'origins': [
        {'origin': 'https://www.linkedin.com', 'localStorage': [{'name': 'token', 'value': 'fake'}]}
    ],
}


class SetupLifecycleTest(unittest.TestCase):
    def test_file_only_and_explicit_database_recovery(self):
        from applicant.services.provider_health import acquire, finish, health
        from tests.research_fixtures import NOW

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder))
            database = Path(folder) / 'chosen.db'
            lease = acquire(database, 'linkedin', clock=lambda: NOW)
            finish(database, 'linkedin', lease['token'], 'needs_setup', clock=lambda: NOW)
            source = Path(folder) / 'input.json'
            source.write_text(json.dumps(STATE), encoding='utf-8')
            result = import_session(
                settings,
                'linkedin',
                source,
                verify=True,
                verifier=Mock(return_value=STATE),
                database=str(database),
            )
            self.assertEqual(result['health_recovery']['status'], 'recovered')
            self.assertEqual(health(database, 'linkedin')['status'], 'ready')
            files = settings.model_copy(update={'store': 'files'})
            result = import_session(
                files,
                'linkedin',
                source,
                replace=True,
                verify=True,
                verifier=Mock(return_value=STATE),
            )
            self.assertEqual(result['health_recovery']['status'], 'skipped_file_only')
            with self.assertRaises(ConfigError):
                import_session(files, 'linkedin', source, database=str(database))

    def test_storage_only_and_invalid_origins(self):
        self.assertEqual(validate_state(STATE, 'linkedin')[0], STATE)
        for origins in ([{'origin': 'https://linkedin.com'}], STATE['origins'] * 2):
            with self.subTest(origins=origins), self.assertRaises(ConfigError):
                validate_state({'cookies': [], 'origins': origins}, 'linkedin')

    def test_paths_and_unsupported_provider(self):
        settings = Settings()
        for provider in ('glassdoor', 'crunchbase', 'tracxn'):
            self.assertEqual(session_path(settings, provider).name, 'storage_state.json')
        with self.assertRaises(ConfigError):
            session_path(settings, 'upwork')
        with self.assertRaises(ConfigError):
            verify_state('upwork', STATE)

    def test_verified_replacement_and_metadata_divergence(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder))
            source = Path(folder) / 'input.json'
            source.write_text(json.dumps(STATE), encoding='utf-8')
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'needs_setup')
            result = import_session(
                settings, 'linkedin', source, verify=True, verifier=Mock(return_value=STATE)
            )
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'verified')
            target = session_path(settings, 'linkedin')
            target.with_suffix('.status.json').write_text('{}', encoding='utf-8')
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'unverified')
            target.with_suffix('.status.json').unlink()
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'unverified')
            previous = target.read_bytes()
            with self.assertRaises(ConfigError):
                import_session(
                    settings,
                    'linkedin',
                    source,
                    verify=True,
                    replace=True,
                    verifier=Mock(return_value={'cookies': [], 'origins': []}),
                )
            self.assertEqual(previous, target.read_bytes())
            with patch('sys.stdin.isatty', return_value=True):
                self.assertEqual(
                    setup_provider(
                        settings, 'linkedin', reauth=True, verifier=Mock(return_value=None)
                    )['status'],
                    'needs_setup',
                )
            source.write_bytes(b' ' * 2_000_001)
            verifier = Mock()
            with self.assertRaises(ConfigError):
                import_session(settings, 'linkedin', source, verifier=verifier)
            verifier.assert_not_called()

    def test_interactive_verification(self):
        session = Mock()
        page = session.start.return_value
        page.url = 'https://linkedin.com/feed/'
        page.title.return_value = 'Feed'
        page.locator.side_effect = lambda selector: Mock(
            count=Mock(return_value=1 if '/mynetwork/' in selector else 0)
        )
        session.context.storage_state.return_value = STATE
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=session)
        factory.return_value.__exit__ = Mock(return_value=None)
        with patch('applicant.services.provider_setup.Terminal') as terminal:
            self.assertEqual(
                verify_state('linkedin', STATE, interactive=True, factory=factory), STATE
            )
            terminal.return_value.pause.assert_called_once()
        factory.assert_called_once_with(headless=False, storage_state=STATE)
