"""Provider metadata and session imports never imply authentication success."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from hypothesis import given
from hypothesis import strategies as st

from applicant.domain.providers import PROVIDERS, provider
from applicant.errors import ConfigError
from applicant.services.provider_setup import (
    import_session,
    session_status,
    setup_provider,
    validate_state,
    verify_state,
)
from applicant.settings import Settings

COOKIE = {'name': 'li_at', 'value': 'synthetic-secret', 'domain': '.linkedin.com', 'path': '/'}


class SessionTest(unittest.TestCase):
    def test_setup_and_status_never_collect_jobs(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder))
            verifier = Mock(return_value={'cookies': [COOKIE], 'origins': []})
            with patch('sys.stdin.isatty', return_value=False):
                self.assertEqual(
                    setup_provider(settings, 'linkedin', verifier=verifier)['status'], 'needs_setup'
                )
                verifier.assert_not_called()
            with patch('sys.stdin.isatty', return_value=True):
                self.assertEqual(
                    setup_provider(settings, 'linkedin', verifier=verifier)['status'], 'verified'
                )
                with self.assertRaises(ConfigError):
                    setup_provider(settings, 'linkedin', verifier=verifier)
                setup_provider(settings, 'linkedin', verifier=verifier, reauth=True)
            self.assertEqual(
                session_status(settings, 'linkedin', check=True, verifier=Mock(return_value=None))[
                    'status'
                ],
                'needs_setup',
            )
            self.assertEqual(
                session_status(settings, 'linkedin', check=True, verifier=verifier)['status'],
                'verified',
            )
            self.assertFalse((Path(folder) / 'applicant.db').exists())

    def test_headless_verifier_detects_redirects_and_missing_markers(self):
        session = Mock()
        page = Mock(url='https://www.linkedin.com/feed/')
        page.title.return_value = 'Feed'
        page.locator.return_value.count.return_value = 0
        session.start.return_value = page
        session.context.storage_state.return_value = {'cookies': [COOKIE], 'origins': []}
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=session)
        factory.return_value.__exit__ = Mock(return_value=None)
        self.assertIsNone(verify_state('linkedin', {}, factory=factory))
        page.url = 'https://www.linkedin.com/authwall'
        self.assertIsNone(verify_state('linkedin', {}, factory=factory))
        page.url = 'https://www.linkedin.com/feed/'

        def locator(selector):
            return Mock(count=Mock(return_value=1 if '/mynetwork/' in selector else 0))

        page.locator.side_effect = locator
        self.assertIsNotNone(verify_state('linkedin', {}, factory=factory))
        factory.assert_called_with(headless=True, storage_state={})

    def test_storage_validation_failure_matrix(self):
        cases = [
            None,
            [],
            {'cookies': 'bad'},
            {'cookies': [], 'origins': 'bad'},
            {'cookies': [dict(COOKIE, expires=float('nan'))]},
            {'cookies': [dict(COOKIE, secure='true')]},
            {'cookies': [dict(COOKIE, sameSite='invalid')]},
            {'cookies': [dict(COOKIE, name='')]},
            {'cookies': [dict(COOKIE, unknown='secret')]},
            {'cookies': [], 'origins': [{'origin': 'https://evil.example', 'localStorage': []}]},
            {'cookies': [], 'origins': [{'origin': None, 'localStorage': []}]},
            {
                'cookies': [],
                'origins': [
                    {
                        'origin': 'https://www.linkedin.com',
                        'localStorage': [{'name': 'a', 'value': 1}],
                    }
                ],
            },
            {
                'cookies': [],
                'origins': [
                    {
                        'origin': 'https://www.linkedin.com',
                        'localStorage': [{'name': 'a', 'value': '1'}, {'name': 'a', 'value': '2'}],
                    }
                ],
            },
            {'cookies': [], 'origins': []},
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ConfigError):
                validate_state(value, 'linkedin')

    def test_catalog_alias_and_no_unimplemented_board(self):
        self.assertEqual(provider('otta').id, 'welcometothejungle')
        self.assertGreater(len(PROVIDERS), 20)
        self.assertIsNone(provider('upwork').board)

    def test_cookie_warning_dry_run_and_failed_replace(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(data_dir=Path(folder))
            source = Path(folder) / 'input.json'
            source.write_text(json.dumps([COOKIE]), encoding='utf-8')
            result = import_session(settings, 'linkedin', source, format='cookies', dry_run=True)
            self.assertEqual(result['warnings'][0]['code'], 'cookie_only_storage_incomplete')
            self.assertFalse((Path(folder) / 'cookies.json').exists())
            import_session(settings, 'linkedin', source, format='cookies')
            before = (Path(folder) / 'cookies.json').read_bytes()
            result = import_session(
                settings,
                'linkedin',
                source,
                format='cookies',
                replace=True,
                verify=True,
                verifier=Mock(return_value=None),
            )
            self.assertEqual(result['status'], 'needs_setup')
            self.assertEqual((Path(folder) / 'cookies.json').read_bytes(), before)
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'unverified')
            with self.assertRaises(ConfigError):
                import_session(settings, 'linkedin', source, format='cookies')

    @given(st.sampled_from(['linkedin.com.evil.test', 'evil.test', 'notlinkedin.com']))
    def test_domain_boundaries(self, host):
        with self.assertRaises(ConfigError):
            validate_state([dict(COOKIE, domain=host)], 'linkedin', 'cookies')

    def test_unknown_storage_rejected(self):
        with self.assertRaises(ConfigError):
            validate_state({'cookies': [], 'sessionStorage': {}}, 'linkedin')
