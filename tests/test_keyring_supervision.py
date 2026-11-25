import os
import signal
import socket
import subprocess
import sys
from contextlib import suppress
from unittest.mock import patch

import pytest

from applicant.infra.owned_worker import WorkerFailure, run_worker
from applicant.infra.session_store import SessionStore, session_keyring
from tests.test_owned_worker import check_native_cleanup


def held_keyring(connection, arguments):
    action, account, value = arguments
    assert action == 'get' and value is None
    port = int(account.split('/')[-1])
    # Keep the original regression runnable against the previous worker protocol.
    connection.send(('operation_ready', None))
    assert connection.recv() is None
    code = (
        'import socket,sys; '
        's=socket.create_connection(("127.0.0.1",int(sys.argv[1]))); '
        's.sendall(b"descendant"); s.settimeout(20); s.recv(1)'
    )
    with socket.create_connection(('127.0.0.1', port), timeout=10) as channel:
        channel.sendall(b'helper')
        with subprocess.Popen([sys.executable, '-c', code, str(port)]):
            channel.settimeout(20)
            channel.recv(1)


def acquisition(connection, arguments):
    path, account, legacy = arguments
    store = SessionStore(path, 'indeed', mode='plaintext')
    with store.execution_gate():
        connection.send(('progress', os.getpid()))
        with patch('applicant.infra.session_store._keyring_worker', held_keyring):
            if legacy:
                from applicant.infra.session_store import _local_session_keyring

                _local_session_keyring('get', account)
            else:
                session_keyring('get', account)


def responsive_keyring(connection, arguments):
    connection.send(('operation_ready', None))
    assert connection.recv() is None
    return 'synthetic result'


def acquisition_read(connection, arguments):
    return session_keyring('get', 'test/reply')


def test_keyring_reply_reaches_waiting_acquisition():
    check_native_cleanup()
    with patch('applicant.infra.session_store._keyring_worker', responsive_keyring):
        assert run_worker(acquisition_read, None) == 'synthetic result'


def available_gate(connection, path):
    with SessionStore(path, 'indeed', mode='plaintext').execution_gate():
        return True


@pytest.mark.parametrize('stop', ['interrupt', 'worker_death'])
@pytest.mark.parametrize(
    'legacy',
    [
        False,
        pytest.param(
            True, marks=pytest.mark.skipif(sys.platform != 'linux', reason='POSIX baseline')
        ),
    ],
    ids=['supervised', 'legacy-nested'],
)
def test_keyring_tree_exits_when_acquisition_stops(tmp_path, stop, legacy):
    check_native_cleanup()
    path = tmp_path / 'session.json'
    channels = {}
    pids = []
    stopped = False

    with socket.socket() as listener, socket.socket() as sentinel:
        sentinel.bind(('127.0.0.1', 0))
        sentinel.listen()
        sentinel.settimeout(10)
        code = 'import socket,sys; s=socket.create_connection(("127.0.0.1", int(sys.argv[1]))); s.recv(1)'
        with (
            subprocess.Popen([sys.executable, '-c', code, str(sentinel.getsockname()[1])]) as other,
            sentinel.accept()[0] as unrelated,
        ):
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            listener.setblocking(False)
            account = f'test/{listener.getsockname()[1]}'

            def check():
                nonlocal stopped
                if stopped:
                    return
                with suppress(BlockingIOError):
                    channel, _ = listener.accept()
                    channel.settimeout(10)
                    with channel.makefile('rb') as stream:
                        prefix = stream.read(1)
                        name = prefix + stream.read(5 if prefix == b'h' else 9)
                    channels[name] = channel
                if channels.keys() == {b'helper', b'descendant'} and pids:
                    stopped = True
                    if stop == 'interrupt':
                        raise KeyboardInterrupt
                    os.kill(pids[0], signal.SIGTERM if os.name == 'nt' else signal.SIGKILL)

            try:
                expected = KeyboardInterrupt if stop == 'interrupt' else WorkerFailure
                with (
                    patch('applicant.infra.session_store._keyring_worker', held_keyring),
                    pytest.raises(expected),
                ):
                    run_worker(
                        acquisition, (path, account, legacy), check=check, progress=pids.append
                    )
                assert stopped, 'acquisition stopped before the keyring descendant was active'
                for channel in channels.values():
                    channel.settimeout(1)
                    if legacy:
                        with pytest.raises(TimeoutError):
                            channel.recv(1)
                    else:
                        with suppress(ConnectionResetError):
                            assert channel.recv(1) == b'', 'owned keyring process is still alive'
                assert other.poll() is None
                assert run_worker(available_gate, path) is True
            finally:
                for channel in channels.values():
                    with suppress(OSError):
                        channel.sendall(b'x')
                    channel.close()
                unrelated.sendall(b'x')
