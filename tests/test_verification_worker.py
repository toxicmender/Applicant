import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from applicant.errors import ConfigError
from applicant.services.provider_setup import (
    _verification_worker,
    _verify_inline,
    import_session,
    session_path,
    session_status,
    setup_provider,
    validate_state,
    verify_state,
)
from applicant.settings import Settings
from tests.test_setup_lifecycle import STATE

TRACXN_DASHBOARD = 'https://platform.tracxn.com/a/dashboard'
TRACXN_MARKER = '#r-dashboard-wrap .txn--home-dashboard'
TRACXN_STATE = {
    'cookies': [],
    'origins': [
        {
            'origin': 'https://platform.tracxn.com',
            'localStorage': [{'name': 'token', 'value': 'fake'}],
        }
    ],
}


class VerificationWorkerTest(unittest.TestCase):
    def test_tracxn_lets_login_redirect_finish_without_second_navigation(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        page = session.start.return_value
        page.url = 'https://tracxn.com/login'
        page.title.return_value = 'Dashboard'
        page.locator.return_value.count.return_value = 0
        session.context.storage_state.return_value = TRACXN_STATE
        pause = Mock()

        def reach_dashboard(matches, *, wait_until, timeout):
            pause.assert_called_once()
            self.assertEqual(wait_until, 'commit')
            self.assertEqual(timeout, 10_000)
            self.assertFalse(matches(page.url))
            self.assertFalse(matches('https://example.com/a/dashboard'))
            self.assertFalse(matches('https://tracxn.com/a/dashboard'))
            self.assertFalse(matches('https://platform.tracxn.com/a/other'))
            page.url = TRACXN_DASHBOARD
            self.assertTrue(matches(page.url))

        page.wait_for_url.side_effect = reach_dashboard
        self.assertEqual(
            _verify_inline('tracxn', None, interactive=True, factory=factory, pause=pause),
            TRACXN_STATE,
        )
        page.goto.assert_called_once_with('https://tracxn.com/login', wait_until='domcontentloaded')
        self.assertEqual(page.locator.call_args.args, (TRACXN_MARKER,))
        page.locator.return_value.first.wait_for.assert_called_once_with(
            state='attached', timeout=10_000
        )

    def test_tracxn_rejects_incomplete_login_and_verifies_saved_state_on_dashboard(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        page = session.start.return_value
        page.url = 'https://tracxn.com/login'
        page.title.return_value = 'Tracxn'
        page.locator.return_value.count.return_value = 0
        page.wait_for_url.side_effect = PlaywrightTimeout('dashboard not reached')
        self.assertIsNone(
            _verify_inline('tracxn', None, interactive=True, factory=factory, pause=Mock())
        )
        session.context.storage_state.assert_not_called()
        page.goto.reset_mock()
        page.url = 'https://example.com/a/dashboard'
        self.assertIsNone(_verify_inline('tracxn', STATE, factory=factory))
        session.context.storage_state.assert_not_called()
        page.goto.assert_called_once_with(TRACXN_DASHBOARD, wait_until='commit')
        page.url = TRACXN_DASHBOARD
        page.locator.return_value.first.wait_for.side_effect = lambda **_: setattr(
            page, 'url', 'https://tracxn.com/login'
        )
        self.assertIsNone(_verify_inline('tracxn', STATE, factory=factory))
        session.context.storage_state.assert_not_called()
        page.url = TRACXN_DASHBOARD + '?return=/login'
        page.locator.return_value.first.wait_for.side_effect = None
        session.context.storage_state.return_value = TRACXN_STATE
        self.assertEqual(_verify_inline('tracxn', TRACXN_STATE, factory=factory), TRACXN_STATE)

    def test_tracxn_setup_saves_provider_state_after_external_login(self):
        factory = MagicMock()
        session = factory.return_value.__enter__.return_value
        page = session.start.return_value
        page.url = TRACXN_DASHBOARD
        page.title.return_value = 'Dashboard'
        page.locator.return_value.count.return_value = 0
        own_cookie = {'name': 'session', 'value': 'fake', 'domain': '.tracxn.com', 'path': '/'}
        external_cookie = dict(own_cookie, domain='.accounts.google.com', value='external-secret')
        external_origin = {'origin': 'https://tracxn.com.example.org', 'localStorage': []}
        captured = {
            'cookies': [own_cookie, external_cookie],
            'origins': [*TRACXN_STATE['origins'], external_origin],
        }
        session.context.storage_state.return_value = captured

        def verify(name, state, *, interactive):
            return _verify_inline(
                name, state, interactive=interactive, factory=factory, pause=Mock()
            )

        # User-supplied state is held to the full validation contract.
        with self.assertRaisesRegex(ConfigError, 'domain is outside'):
            validate_state(captured, 'tracxn')
        with (
            tempfile.TemporaryDirectory() as folder,
            patch('sys.stdin.isatty', return_value=True),
            self.assertLogs('applicant.browser', 'DEBUG') as logs,
        ):
            settings = Settings(data_dir=Path(folder), store='files')
            result = setup_provider(settings, 'tracxn', verifier=verify)
            saved = json.loads(session_path(settings, 'tracxn').read_text(encoding='utf-8'))
        expected, _ = validate_state({**TRACXN_STATE, 'cookies': [own_cookie]}, 'tracxn')
        self.assertEqual(saved, expected)
        self.assertEqual(result['status'], 'verified')
        output = '\n'.join(logs.output)
        self.assertIn("'excluded_cookies': 1, 'excluded_origins': 1", output)
        self.assertNotIn('external-secret', output)

    def test_verification_dispatch_and_protocol(self):
        state = {'cookies': [], 'origins': []}
        with patch('applicant.infra.owned_worker.run_worker', return_value=state) as worker:
            self.assertIs(verify_state('linkedin', state, interactive=True), state)
            self.assertEqual(
                worker.call_args.args, (_verification_worker, ('linkedin', state, True))
            )
            self.assertTrue(worker.call_args.kwargs['interactive'])
        connection = Mock()

        def inline(name, state, *, factory, pause, interactive):
            self.assertEqual((name, interactive), ('linkedin', True))
            pause('login prompt')
            session = factory(storage_state=state)
            session.__exit__(None, None, None)
            return state

        with patch('applicant.services.provider_setup._verify_inline', side_effect=inline):
            self.assertIs(_verification_worker(connection, ('linkedin', state, True)), state)
        self.assertEqual(connection.send.call_args_list[0].args[0], ('pause', 'login prompt'))
        connection.recv.assert_called_once()
        connection.send.assert_called_with(('closing', None))

    def test_database_contention_preserves_installed_verified_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, database = root / 'incoming.json', root / 'existing.db'
            source.write_text(json.dumps(STATE), encoding='utf-8')
            database.touch()
            settings = Settings(data_dir=root)
            with patch(
                'applicant.services.provider_health.sqlite3.connect',
                side_effect=sqlite3.OperationalError('database is locked'),
            ):
                result = import_session(
                    settings,
                    'linkedin',
                    source,
                    verify=True,
                    verifier=Mock(return_value=STATE),
                    database=str(database),
                )
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(
                result['health_recovery'], {'status': 'database_busy', 'retryable': True}
            )
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'verified')
