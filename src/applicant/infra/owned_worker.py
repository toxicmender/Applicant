"""Private IPC and bounded cleanup for owned browser process trees."""

import ctypes
import json
import logging
import multiprocessing
import os
import signal
import struct
import sys
import time
from contextlib import suppress
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any

from ..errors import SourceError
from .browser_diagnostics import QUEUE_SIZE, WorkerDiagnostics, active_diagnostics, failure_details
from .interrupts import defer_interrupt

logger = logging.getLogger('applicant.browser.worker')


class WorkerFailure(SourceError):
    """The worker failed or could not finish normal teardown."""


class CleanupUnconfirmed(WorkerFailure):
    cleanup_unconfirmed = True


class _PromptFinished(Exception):
    """The worker ended an outstanding terminal prompt."""


def _protect_connection(connection):
    if os.name == 'nt':
        os.set_handle_inheritable(connection.fileno(), False)
        inheritable = os.get_handle_inheritable(connection.fileno())
    else:
        os.set_inheritable(connection.fileno(), False)
        inheritable = os.get_inheritable(connection.fileno())
    if inheritable:
        raise OSError('browser control pipe is inheritable')


def _group_has_live_members(pid):
    if os.name == 'nt':
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    if sys.platform != 'linux' or not Path('/proc/self/stat').is_file():
        return True
    # A killed orphan can remain a zombie until init reaps it. It owns no handles.
    for stat in Path('/proc').glob('[0-9]*/stat'):
        try:
            state, _, group, *_ = stat.read_text().rpartition(') ')[2].split()
        except (OSError, ValueError):
            continue
        if group == str(pid) and state not in ('Z', 'X'):
            return True
    return False


class _ControlPipe:
    """Complete messages cross thread queues; only the I/O threads touch the pipe."""

    def __init__(self, connection):
        self.incoming = Queue()
        self.outgoing = Queue()
        self.connection = connection
        Thread(target=self._read, daemon=True).start()
        Thread(target=self._write, daemon=True).start()

    def _read(self):
        try:
            while True:
                self.incoming.put(self.connection.recv())
        except (EOFError, OSError, ValueError):
            self.incoming.put(('eof', None))

    def _write(self):
        try:
            while (message := self.outgoing.get()) is not None:
                self.connection.send(message[0])
        except (EOFError, OSError, ValueError):
            self.incoming.put(('eof', None))

    def send(self, value):
        self.outgoing.put((value,))

    def receive(self, timeout=0.05):
        try:
            return self.incoming.get(timeout=timeout)
        except Empty:
            return None

    def close(self):
        self.outgoing.put(None)
        self.connection.close()


class WindowsJob:
    kernel: Any  # ctypes resolves Win32 functions and their signatures at runtime.
    handle: int | None

    def __init__(self, pid):
        if sys.platform != 'win32':
            raise WorkerFailure('Windows Job Objects require Windows')
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel
        self.handle = kernel.CreateJobObjectW(None, None)
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION: LimitFlags is at byte 16.
        limits = ctypes.create_string_buffer(144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)
        struct.pack_into('I', limits, 16, 0x2000)  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, limits, len(limits)):
            kernel.CloseHandle(self.handle)
            raise OSError(ctypes.get_last_error(), 'could not configure owned worker job')
        process = kernel.OpenProcess(0x0100 | 0x0001, False, pid)
        try:
            if (
                not self.handle
                or not process
                or not kernel.AssignProcessToJobObject(self.handle, process)
            ):
                code = ctypes.get_last_error()
                kernel.CloseHandle(self.handle)
                raise OSError(code, 'could not assign owned worker job')
        finally:
            if process:
                kernel.CloseHandle(process)

    def terminate(self, deadline=None):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise CleanupUnconfirmed('could not terminate owned worker job')
        deadline = time.monotonic() + 5 if deadline is None else deadline
        while True:
            accounting = ctypes.create_string_buffer(48)
            if not self.kernel.QueryInformationJobObject(
                self.handle, 1, accounting, len(accounting), None
            ):
                raise CleanupUnconfirmed('could not confirm owned process tree cleanup')
            if struct.unpack_from('I', accounting, 40)[0] == 0:
                return
            if time.monotonic() >= deadline:
                raise CleanupUnconfirmed('owned process tree cleanup exceeded deadline')
            time.sleep(0.05)

    def close(self):
        self.kernel.CloseHandle(self.handle)


def _child(connection, function, diagnostics_queue, provider):
    _protect_connection(connection)
    if os.name != 'nt':
        os.setsid()
    connection.send(('ready', None))
    diagnostics = WorkerDiagnostics(diagnostics_queue, provider)
    token = active_diagnostics.set(diagnostics)
    try:
        arguments = connection.recv()
        kind, value = 'result', function(connection, arguments)
    except BaseException as error:  # noqa: BLE001 - relay worker termination without secrets
        kind = 'error'
        captured = diagnostics.failures.get(id(error))
        value = captured[1] if captured else failure_details(error, 'worker')
        if diagnostics.cleanup_failures:
            value = {**value, 'cleanup_failures': diagnostics.cleanup_failures}
    finally:
        active_diagnostics.reset(token)
    try:
        connection.send(
            (kind, (value, diagnostics.emitted, diagnostics.dropped, diagnostics.discarded))
        )
    finally:
        diagnostics_queue.close()
        connection.close()


def _drain_diagnostics(queue) -> int:
    count = 0
    for _ in range(QUEUE_SIZE):
        try:
            record = json.loads(queue.get_nowait())
        except (Empty, EOFError, OSError, TypeError, ValueError):
            break
        logger.log(record.pop('severity'), 'browser diagnostic %s', record)
        count += 1
    return count


def run_worker(
    function,
    arguments,
    *,
    interactive=False,
    pause=None,
    prompt=None,
    check=None,
    progress=None,
    timeout: float | None = 60,
    grace: float = 5,
    monotonic=time.monotonic,
    provider: str = 'unknown',
):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    diagnostics = context.Queue(maxsize=QUEUE_SIZE)
    process = context.Process(target=_child, args=(child, function, diagnostics, provider))
    try:
        _protect_connection(parent)
        _protect_connection(child)
        process.start()
    except BaseException:
        parent.close()
        child.close()
        diagnostics.close()
        process.close()
        raise
    pid = process.pid
    assert pid is not None  # A successfully started process owns a PID.
    child.close()
    job = None
    pipe = None
    deadline = None if timeout is None else monotonic() + timeout
    group_ready = False
    emitted = dropped = discarded = None
    received = 0
    failure = None
    closing = None
    pending = None
    try:
        pipe = _ControlPipe(parent)
        startup_deadline = time.monotonic() + 10
        while (ready := pipe.receive()) is None and time.monotonic() < startup_deadline:
            if not process.is_alive():
                break
        if ready != ('ready', None):
            raise WorkerFailure('browser worker startup failed')
        group_ready = os.name != 'nt'
        if os.name == 'nt':
            job = WindowsJob(pid)
        pipe.send(arguments)
        while True:
            received += _drain_diagnostics(diagnostics)
            if check:
                check()
            if deadline is not None and monotonic() >= deadline:
                raise WorkerFailure('browser worker deadline exceeded')
            message = pending or pipe.receive()
            pending = None
            if message is not None:
                kind, value = message
                if kind == 'eof':
                    raise WorkerFailure('browser worker exited without a result')
                if kind == 'pause':
                    if pause is None or not interactive:
                        raise WorkerFailure('unexpected interactive worker request')
                    received += _drain_diagnostics(diagnostics)
                    pause(value)
                    pipe.send(None)
                    deadline = None if timeout is None else monotonic() + timeout
                elif kind == 'prompt':
                    if prompt is None or not interactive:
                        raise WorkerFailure('unexpected interactive worker prompt')
                    deadline = None  # A live interactive prompt may take as long as the user needs.
                    heartbeat_deadline = monotonic() + 5

                    def poll_prompt():
                        nonlocal heartbeat_deadline, pending
                        if check:
                            check()
                        response = pipe.receive()
                        if response is not None:
                            if response[0] == 'heartbeat':
                                heartbeat_deadline = monotonic() + 5
                            else:
                                pending = response
                                raise _PromptFinished
                        if not process.is_alive():
                            raise WorkerFailure('browser worker exited without a result')
                        if monotonic() >= heartbeat_deadline:
                            raise WorkerFailure('browser worker stopped responding during a prompt')

                    try:
                        answer = prompt(value, poll_prompt)
                    except _PromptFinished:
                        continue
                    pipe.send(answer)
                    deadline = None if timeout is None else monotonic() + timeout
                elif kind == 'heartbeat':
                    continue  # A heartbeat can race with a submitted answer.
                elif kind == 'closing':
                    closing = time.monotonic() + grace
                    deadline = monotonic() + grace
                elif kind == 'progress' and progress is not None:
                    progress(value)
                elif kind in ('error', 'result'):
                    result, emitted, dropped, discarded = value
                    if kind == 'error':
                        logger.debug(
                            'browser worker error provider=%s pid=%s %s', provider, pid, result
                        )
                        failure = WorkerFailure(
                            'browser worker failed: {exception_class} during {operation}'.format(
                                **result
                            )
                            + (f' ({result["network_code"]})' if result['network_code'] else '')
                        )
                    break
            elif not process.is_alive():
                raise WorkerFailure('browser worker exited without a result')
        # The child can only finish flushing its queue while the parent drains it.
        flush_deadline = closing if closing is not None else time.monotonic() + grace
        while process.is_alive() and time.monotonic() < flush_deadline:
            received += _drain_diagnostics(diagnostics)
            process.join(min(0.05, max(0.0, flush_deadline - time.monotonic())))
        received += _drain_diagnostics(diagnostics)
        if failure:
            if process.is_alive():
                logger.warning('browser worker teardown exceeded deadline after failure')
            raise failure
        if process.is_alive():
            raise WorkerFailure('browser worker teardown exceeded deadline')
        return result
    except EOFError as error:
        raise WorkerFailure('browser worker exited without a result') from error
    finally:
        with defer_interrupt():
            original = sys.exc_info()[1]
            try:
                if emitted is None:
                    logger.debug(
                        'browser diagnostics incomplete provider=%s pid=%s final_drop_count=unknown',
                        provider,
                        pid,
                    )
                else:
                    if dropped:
                        logger.warning('Diagnostic queue saturated: %d events dropped', dropped)
                    logger.debug(
                        'browser diagnostic summary provider=%s pid=%s emitted=%s received=%s '
                        'dropped=%s discarded=%s',
                        provider,
                        pid,
                        emitted,
                        received,
                        dropped,
                        discarded,
                    )
                    if received < emitted:
                        logger.debug(
                            'browser diagnostic delivery incomplete provider=%s pid=%s',
                            provider,
                            pid,
                        )
                logger.debug('browser worker cleanup started provider=%s pid=%s', provider, pid)
                if original is not None:
                    logger.debug('browser worker failure cleanup provider=%s pid=%s', provider, pid)
                if (
                    prompt is not None
                    and pipe is not None
                    and closing is None
                    and process.is_alive()
                ):
                    pipe.send(None)  # Cancel a prompt in the worker and allow normal browser close.
                    graceful_deadline = time.monotonic() + grace
                    while process.is_alive() and time.monotonic() < graceful_deadline:
                        message = pipe.receive()
                        if (
                            message is not None
                            and message[0] == 'progress'
                            and progress is not None
                        ):
                            progress(message[1])
                        received += _drain_diagnostics(diagnostics)
                forced_deadline = time.monotonic() + 5
                if job:
                    job.terminate(forced_deadline)
                elif os.name != 'nt' and group_ready:
                    with suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                elif process.is_alive():
                    process.terminate()
                if process.is_alive():
                    process.join(max(0, forced_deadline - time.monotonic()))
                if process.is_alive():
                    raise CleanupUnconfirmed('browser cleanup could not be confirmed')
                if group_ready:
                    while _group_has_live_members(pid):
                        if time.monotonic() >= forced_deadline:
                            raise CleanupUnconfirmed('owned process group did not terminate')
                        time.sleep(0.05)
                logger.debug('browser worker cleanup completed provider=%s pid=%s', provider, pid)
            except (OSError, CleanupUnconfirmed) as error:
                logger.error('browser worker cleanup unconfirmed provider=%s pid=%s', provider, pid)
                message = f'browser cleanup could not be confirmed for worker PID {pid}'
                if isinstance(original, WorkerFailure):
                    message = f'{original}; {message}'
                raise CleanupUnconfirmed(message) from (original or error)
            finally:
                if job:
                    job.close()
                if pipe is None:
                    parent.close()
                else:
                    pipe.close()
                diagnostics.close()
                if not process.is_alive():
                    process.close()
