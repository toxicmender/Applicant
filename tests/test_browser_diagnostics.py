import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from queue import Queue
from unittest.mock import MagicMock, Mock

from playwright.sync_api import Error
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from applicant import log
from applicant.infra.browser import _observe_browser, _observe_page
from applicant.infra.browser_diagnostics import (
    WorkerDiagnostics,
    active_diagnostics,
    event,
    safe_origin,
)
from applicant.infra.owned_worker import _drain_diagnostics
from applicant.services.provider_setup import _verify_inline


class BrowserDiagnosticsTest(unittest.TestCase):
    def test_url_origin(self):
        cases = {
            'https://user:password@TRACXN.COM:8443/private?token=secret#private': 'https://tracxn.com:8443',
            'http://[2001:db8::1]:8888/x': 'http://[2001:db8::1]:8888',
            'https://example.com:bad/': 'unknown',
            'https://[broken/': 'unknown',
            'file:///private': 'unknown',
            'https:///missing-host': 'unknown',
            'https://a%20b.example': 'unknown',
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(safe_origin(url), expected)

    def test_playwright_events_use_captured_sink_and_filter_http_failures(self):
        page, browser = Mock(), Mock()
        queue = Queue()
        token = active_diagnostics.set(WorkerDiagnostics(queue, 'tracxn'))
        try:
            _observe_page(page)
            _observe_browser(browser)
        finally:
            active_diagnostics.reset(token)
        callbacks = dict(call.args for call in page.on.call_args_list)
        url = 'https://user:secret@tracxn.com/private?token=secret#secret'
        for status, navigation, frame in (
            (200, True, page.main_frame),
            (302, True, page.main_frame),
            (500, False, page.main_frame),
            (404, True, Mock()),
            (403, True, page.main_frame),
            (404, True, page.main_frame),
            (500, True, page.main_frame),
        ):
            response = Mock(status=status, frame=frame, url=url)
            response.request.is_navigation_request.return_value = navigation
            callbacks['response'](response)
        callbacks['requestfailed'](Mock(failure='net::ERR_CONNECTION_RESET', url=url))
        callbacks['requestfailed'](Mock(failure='net::ERR_SECRET', url=url))
        callbacks['crash'](page)
        browser.on.call_args.args[1](browser)
        records = [json.loads(queue.get_nowait()) for _ in range(6)]
        self.assertTrue(queue.empty())
        self.assertEqual([row['status'] for row in records[:3]], [403, 404, 500])
        self.assertEqual(records[3]['network_code'], 'ERR_CONNECTION_RESET')
        self.assertEqual([row['outcome'] for row in records[4:]], ['crashed', 'disconnected'])
        self.assertTrue(all(row['origin'] == 'https://tracxn.com' for row in records[:4]))
        self.assertNotIn('secret', json.dumps(records))

    def test_setup_reports_marker_missing_and_export_failure(self):
        session, page = Mock(), Mock(url='https://platform.tracxn.com/a/dashboard')
        session.start.return_value = page
        factory = MagicMock()
        factory.return_value.__enter__.return_value = session
        page.title.return_value = 'Access denied'
        with self.assertLogs('applicant.browser', 'DEBUG') as logs:
            self.assertIsNone(_verify_inline('tracxn', None, factory=factory))
        self.assertIn('authentication_check blocked', '\n'.join(logs.output))
        page.title.return_value = 'Home'
        marker = Mock()
        marker.first.wait_for.side_effect = PlaywrightTimeout('marker not attached')
        page.locator.side_effect = lambda selector: (
            marker
            if selector == '#r-dashboard-wrap .txn--home-dashboard'
            else Mock(count=Mock(return_value=0))
        )
        with self.assertLogs('applicant.browser', 'DEBUG') as logs:
            self.assertIsNone(_verify_inline('tracxn', None, factory=factory))
        self.assertIn('marker_missing', '\n'.join(logs.output))
        marker.first.wait_for.side_effect = None
        session.context.storage_state.side_effect = Error('sensitive content')
        with self.assertLogs('applicant.browser', 'DEBUG') as logs, self.assertRaises(Error):
            _verify_inline('tracxn', None, factory=factory)
        output = '\n'.join(logs.output)
        self.assertIn('storage_export failed', output)
        self.assertNotIn('sensitive content', output)

    def test_existing_run_log_and_console_controls(self):
        queue = Queue()
        diagnostics = WorkerDiagnostics(queue, 'tracxn')
        with tempfile.TemporaryDirectory() as folder:
            self.addCleanup(log.silence)
            path = Path(folder) / 'setup.log'
            console = io.StringIO()
            for _ in range(2):
                log.configure(verbosity=log.QUIET, stream=console, filepath=str(path))
            diagnostics.emit('login_navigation', 'started', logging.DEBUG, {})
            self.assertEqual(_drain_diagnostics(queue), 1)
            self.assertEqual(console.getvalue(), '')
            self.assertEqual(path.read_text(encoding='utf-8').count('login_navigation'), 1)
            log.configure(verbosity=1, stream=console)
            event('authentication_check', 'completed')
            self.assertIn('authentication_check', console.getvalue())
            self.assertNotIn('authentication_check', path.read_text(encoding='utf-8'))
            log.silence()
