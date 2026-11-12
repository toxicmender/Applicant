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
from queue import Empty
from typing import Any

from ..errors import SourceError
from .browser_diagnostics import QUEUE_SIZE, WorkerDiagnostics, active_diagnostics, failure_details

logger = logging.getLogger('applicant.browser.worker')


class WorkerFailure(SourceError):
    """The worker failed or could not finish normal teardown."""


class CleanupUnconfirmed(WorkerFailure):
    cleanup_unconfirmed = True


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

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise CleanupUnconfirmed('could not terminate owned worker job')
        deadline = time.monotonic() + 5
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
    if os.name != 'nt':
        os.setsid()
    connection.send(('ready', None))
    diagnostics = WorkerDiagnostics(diagnostics_queue, provider)
    token = active_diagnostics.set(diagnostics)
    try:
        kind, value = 'result', function(connection, connection.recv())
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
    deadline = None if timeout is None else monotonic() + timeout
    group_ready = False
    emitted = dropped = discarded = None
    received = 0
    failure = None
    try:
        if not parent.poll(10) or parent.recv()[0] != 'ready':
            raise WorkerFailure('browser worker startup failed')
        group_ready = os.name != 'nt'
        if os.name == 'nt':
            job = WindowsJob(pid)
        parent.send(arguments)
        while True:
            received += _drain_diagnostics(diagnostics)
            if check:
                check()
            if deadline is not None and monotonic() >= deadline:
                raise WorkerFailure('browser worker deadline exceeded')
            if parent.poll(0.1):
                kind, value = parent.recv()
                if kind == 'pause':
                    if pause is None or not interactive:
                        raise WorkerFailure('unexpected interactive worker request')
                    received += _drain_diagnostics(diagnostics)
                    pause(value)
                    parent.send(None)
                    deadline = None if timeout is None else monotonic() + timeout
                elif kind == 'closing':
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
        flush_deadline = time.monotonic() + grace
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
                        'browser diagnostic delivery incomplete provider=%s pid=%s', provider, pid
                    )
            logger.debug('browser worker cleanup started provider=%s pid=%s', provider, pid)
            if original is not None:
                logger.debug('browser worker failure cleanup provider=%s pid=%s', provider, pid)
            if job:
                job.terminate()
            elif os.name != 'nt' and group_ready:
                with suppress(ProcessLookupError):
                    os.killpg(pid, signal.SIGKILL)
            elif process.is_alive():
                process.terminate()
            if process.is_alive():
                process.join(5)
            if process.is_alive():
                raise CleanupUnconfirmed('browser cleanup could not be confirmed')
            logger.debug('browser worker cleanup completed provider=%s pid=%s', provider, pid)
        except (OSError, CleanupUnconfirmed) as error:
            logger.error('browser worker cleanup unconfirmed provider=%s pid=%s', provider, pid)
            if isinstance(original, WorkerFailure):
                raise CleanupUnconfirmed(
                    f'{original}; browser cleanup could not be confirmed'
                ) from original
            raise CleanupUnconfirmed('browser cleanup could not be confirmed') from (
                original or error
            )
        finally:
            if job:
                job.close()
            parent.close()
            diagnostics.close()
            if not process.is_alive():
                process.close()
