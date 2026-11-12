import ctypes
import errno
import multiprocessing
import os
import signal
import socket
import subprocess
import sys
import unittest
from contextlib import suppress
from unittest.mock import Mock, patch

from playwright.sync_api import Error

from applicant.infra.browser_diagnostics import QUEUE_SIZE, event, stage
from applicant.infra.owned_worker import WorkerFailure, run_worker


def echo(connection, arguments):
    connection.send(('closing', None))
    return arguments


def hanging(connection, arguments):
    connection.send(('closing', None))
    connection.recv()


def failing(connection, arguments):
    raise ValueError('private data must not leave the worker')


def overflowing(connection, address):
    connection.send(('pause', 'fill diagnostic queue'))
    with socket.create_connection(address, timeout=10) as signal_socket:
        try:
            with stage('verification_navigation'):
                for _ in range(QUEUE_SIZE + 12):
                    event('request', 'failed')
                raise Error('navigation net::ERR_CONNECTION_RESET at ?token=private-value')
        finally:
            signal_socket.sendall(b'1')
            connection.recv()


def exit_abruptly(connection, arguments):
    os._exit(5)


def failing_during_cleanup(connection, arguments):
    try:
        with stage('login_navigation'):
            raise Error('net::ERR_NAME_NOT_RESOLVED')
    finally:
        with suppress(RuntimeError), stage('browser_teardown'):
            raise RuntimeError('private cleanup details')


def held_child(connection):
    if os.name != 'nt':
        try:
            os.setsid()
        except OSError as error:
            connection.send(('setsid', error.errno))
            return
    connection.send(('ready', None))
    connection.recv()


def held_tree(connection, address):
    code = (
        'import socket,sys; '
        's=socket.create_connection((sys.argv[1],int(sys.argv[2]))); '
        's.sendall(b"ready"); s.recv(1)'
    )
    with subprocess.Popen([sys.executable, '-c', code, *map(str, address)]):
        connection.send(('pause', 'descendant started'))
        connection.recv()


def probe_windows_job(pid):
    if sys.platform != 'win32':
        raise AssertionError('Windows job probe requires Windows')
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), 'CreateJobObjectW')
    process = kernel.OpenProcess(0x0101, False, pid)
    try:
        if not process:
            raise OSError(ctypes.get_last_error(), 'OpenProcess')
        if not kernel.AssignProcessToJobObject(job, process):
            raise OSError(ctypes.get_last_error(), 'AssignProcessToJobObject')
        if not kernel.TerminateJobObject(job, 1):
            raise OSError(ctypes.get_last_error(), 'TerminateJobObject')
    finally:
        if process:
            kernel.CloseHandle(process)
        kernel.CloseHandle(job)


def check_native_cleanup():
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=held_child, args=(child,))
    process.start()
    child.close()
    try:
        if not parent.poll(60):
            raise AssertionError('native capability probe did not reach ready')
        operation, code = parent.recv()
        if operation != 'ready':
            raise OSError(code, operation)
        assert process.pid is not None
        if os.name == 'nt':
            probe_windows_job(process.pid)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.join(60)
        if process.is_alive():
            raise AssertionError('native capability probe did not terminate')
    finally:
        parent.close()
        if process.is_alive():
            process.kill()
            process.join(60)
        process.close()


class OwnedWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            check_native_cleanup()
        except OSError as error:
            restriction = error.errno in (
                {5} if os.name == 'nt' else {errno.EPERM, errno.EACCES, errno.ENOSYS}
            )
            if not restriction or os.environ.get('APPLICANT_REQUIRE_NATIVE_CLEANUP') == '1':
                raise
            raise unittest.SkipTest(f'native capability restriction: {error}') from error

    def test_private_roundtrip(self):
        self.assertEqual(run_worker(echo, {'synthetic': 'state'}), {'synthetic': 'state'})

    def test_hung_close_is_bounded(self):
        # The injected clock advances only after the closing message has arrived.
        clock = Mock(side_effect=[0, 0, 0, 6])
        with self.assertRaisesRegex(WorkerFailure, 'deadline'):
            run_worker(hanging, None, timeout=None, grace=5, monotonic=clock)

    def test_failure_does_not_disclose_payload(self):
        with self.assertRaisesRegex(WorkerFailure, 'ValueError') as raised:
            run_worker(failing, None)
        self.assertNotIn('private data', str(raised.exception))

    def test_saturated_queue_preserves_failure_and_drop_summary(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.settimeout(15)

            def pause(message):
                self.assertEqual(message, 'fill diagnostic queue')
                with listener.accept()[0] as channel:
                    channel.settimeout(15)
                    self.assertEqual(channel.recv(1), b'1')

            with (
                self.assertLogs('applicant.browser.worker', level='DEBUG') as logs,
                self.assertRaisesRegex(
                    WorkerFailure, 'verification_navigation.*ERR_CONNECTION_RESET'
                ),
            ):
                run_worker(
                    overflowing,
                    listener.getsockname(),
                    interactive=True,
                    pause=pause,
                    provider='tracxn',
                )
        text = '\n'.join(logs.output)
        self.assertIn('Diagnostic queue saturated: 14 events dropped', text)
        self.assertIn(f'emitted={QUEUE_SIZE} received={QUEUE_SIZE} dropped=14', text)
        self.assertIn("'provider': 'tracxn'", text)
        self.assertNotIn('private-value', text)

    def test_abrupt_exit_leaves_final_drop_count_unknown(self):
        with (
            self.assertLogs('applicant.browser.worker', level='DEBUG') as logs,
            self.assertRaisesRegex(WorkerFailure, 'exited without a result'),
        ):
            run_worker(exit_abruptly, None, provider='tracxn')
        self.assertIn('final_drop_count=unknown', '\n'.join(logs.output))

    def test_cleanup_failure_preserves_original_browser_stage(self):
        with (
            self.assertLogs('applicant.browser.worker', level='DEBUG') as logs,
            self.assertRaisesRegex(WorkerFailure, 'login_navigation.*ERR_NAME_NOT_RESOLVED'),
        ):
            run_worker(failing_during_cleanup, None, provider='tracxn')
        text = '\n'.join(logs.output)
        self.assertIn("'cleanup_failures': [{'operation': 'browser_teardown'", text)
        self.assertNotIn('private cleanup details', text)

    def test_interrupt_terminates_only_owned_tree(self):
        context = multiprocessing.get_context('spawn')
        control, sentinel_pipe = context.Pipe()
        sentinel = context.Process(target=held_child, args=(sentinel_pipe,))
        sentinel.start()
        sentinel_pipe.close()
        descendant = None
        try:
            self.assertTrue(control.poll(60), 'unrelated sentinel did not reach ready')
            self.assertEqual(control.recv(), ('ready', None))
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                listener.settimeout(60)

                def cancel(phase):
                    nonlocal descendant
                    self.assertEqual(phase, 'descendant started')
                    descendant, _ = listener.accept()
                    descendant.settimeout(60)
                    with descendant.makefile('rb') as stream:
                        self.assertEqual(stream.read(5), b'ready')
                    raise KeyboardInterrupt

                with self.assertRaises(KeyboardInterrupt):
                    run_worker(
                        held_tree,
                        listener.getsockname(),
                        interactive=True,
                        pause=cancel,
                    )
            assert descendant is not None
            # Windows can reset an established socket on forced process exit.
            with suppress(ConnectionResetError):
                self.assertEqual(descendant.recv(1), b'')
            self.assertTrue(sentinel.is_alive())
        finally:
            if descendant is not None:
                descendant.close()
            if sentinel.is_alive():
                control.send(None)
                sentinel.join(60)
            control.close()
            if sentinel.is_alive():
                sentinel.kill()
                sentinel.join(60)
            sentinel.close()


class OwnedWorkerBoundaryTest(unittest.TestCase):
    def test_spawn_failure_closes_both_pipe_ends(self):
        context = Mock()
        parent, child = Mock(), Mock()
        context.Pipe.return_value = parent, child
        context.Process.return_value.start.side_effect = OSError('spawn failed')
        with (
            patch('applicant.infra.owned_worker.multiprocessing.get_context', return_value=context),
            self.assertRaisesRegex(OSError, 'spawn failed'),
        ):
            run_worker(echo, None)
        parent.close.assert_called_once_with()
        child.close.assert_called_once_with()
        context.Process.return_value.close.assert_called_once_with()

    def test_missing_startup_handshake_terminates_only_worker(self):
        context = Mock()
        parent, child = Mock(), Mock()
        context.Pipe.return_value = parent, child
        parent.poll.return_value = False
        process = context.Process.return_value
        process.pid = 42
        process.is_alive.side_effect = [True, True, False, False]
        with (
            patch('applicant.infra.owned_worker.multiprocessing.get_context', return_value=context),
            self.assertRaisesRegex(WorkerFailure, 'startup failed'),
        ):
            run_worker(echo, None)
        parent.poll.assert_called_once_with(10)
        process.terminate.assert_called_once_with()
        process.join.assert_called_once_with(5)
        parent.close.assert_called_once_with()
        child.close.assert_called_once_with()
