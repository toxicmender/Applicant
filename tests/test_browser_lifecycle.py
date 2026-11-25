import socket
import unittest
from contextlib import suppress
from time import monotonic, sleep
from unittest.mock import Mock, patch

from applicant.infra.owned_worker import run_worker
from tests.test_owned_worker import check_native_cleanup


def board_close_worker(connection, arguments):
    from applicant.infra.board_worker import _collect
    from applicant.infra.browser import BrowserSession, browser
    from applicant.infra.session_access import SessionExpired, active_access

    request, mode, address = arguments

    class Browser:
        def close(self):
            if mode in ('timeout', 'anonymous', 'interrupt'):
                with socket.create_connection(address, timeout=10) as channel:
                    channel.sendall(b'ready')
                    channel.recv(1)
            elif mode == 'slow':
                sleep(0.5)

    class Board:
        def search(self):
            if mode == 'anonymous' and active_access.get() is not None:
                raise SessionExpired('synthetic sign-out')
            for _ in range(2):
                with browser():
                    pass
            return []

        def close(self):
            pass

    def start(session):
        session._closer = Browser()
        page = Mock()
        page.url = 'https://www.naukri.com/jobs'
        page.title.return_value = 'Jobs'
        page.locator.return_value.count.return_value = 0
        session._page = page
        session._context = Mock()
        session._context.storage_state.return_value = session.storage_state
        return page

    with (
        patch.object(BrowserSession, 'start', start),
        patch('applicant.infra.board_worker.board_client', return_value=Board()),
    ):
        return _collect(connection, request)


def acquire_board_gate(connection, path):
    from applicant.infra.session_store import SessionStore

    with SessionStore(path, 'naukri', mode='plaintext').execution_gate():
        return True


class BrowserLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        check_native_cleanup()

    def test_managed_board_teardown_is_bounded_and_releases_gate(self):
        for mode in ('timeout', 'anonymous', 'interrupt', 'normal', 'slow'):
            with self.subTest(mode=mode):
                self.exercise_board_close(mode)

    def exercise_board_close(self, mode):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        from applicant.infra.owned_worker import WorkerTeardownTimeout
        from applicant.services.provider_setup import provider_session
        from applicant.settings import Settings

        with TemporaryDirectory() as folder, socket.socket() as listener:
            settings = Settings(data_dir=Path(folder), session_storage='plaintext')
            store = provider_session(settings, 'naukri')
            store.install(
                {},
                {
                    'cookies': [
                        {
                            'name': 'test',
                            'value': 'fixture',
                            'domain': '.naukri.com',
                            'path': '/',
                        }
                    ],
                    'origins': [],
                },
                'verified',
            )
            request = ('naukri', 'search', (), {}, True, settings.model_dump())
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.setblocking(False)
            connections = []
            close_observed = None

            def check():
                nonlocal close_observed
                if connections:
                    return
                with suppress(BlockingIOError):
                    channel, _ = listener.accept()
                    connections.append(channel)
                    channel.settimeout(5)
                    with channel.makefile('rb') as stream:
                        self.assertEqual(stream.read(5), b'ready')
                    close_observed = monotonic()
                    if mode == 'interrupt':
                        raise KeyboardInterrupt

            args = (request, mode, listener.getsockname())
            try:
                if mode in ('normal', 'slow'):
                    self.assertEqual(
                        run_worker(
                            board_close_worker, args, timeout=None, check=check, close_timeout=2
                        )['error'],
                        None,
                    )
                elif mode == 'interrupt':
                    with self.assertRaises(KeyboardInterrupt):
                        run_worker(
                            board_close_worker, args, timeout=None, check=check, close_timeout=2
                        )
                else:
                    startup_deadline = monotonic() + 60

                    def watchdog():
                        check()
                        if close_observed is None and monotonic() > startup_deadline:
                            raise AssertionError('worker never reached browser close')
                        if close_observed is not None and monotonic() - close_observed > 10:
                            raise AssertionError('browser-close deadline was never applied')

                    with self.assertRaises(WorkerTeardownTimeout):
                        run_worker(
                            board_close_worker, args, timeout=None, check=watchdog, close_timeout=2
                        )
                if mode in ('timeout', 'anonymous', 'interrupt'):
                    self.assertEqual(len(connections), 1)
                    with suppress(ConnectionResetError):
                        self.assertEqual(connections[0].recv(1), b'')
                self.assertTrue(run_worker(acquire_board_gate, store.path))
                self.assertEqual(
                    store.snapshot()['status'],
                    'needs_setup' if mode == 'anonymous' else 'verified',
                )
            finally:
                for channel in connections:
                    channel.close()
