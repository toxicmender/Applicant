"""Private IPC and bounded cleanup for owned browser process trees."""

import ctypes
import multiprocessing
import os
import signal
import struct
import time
from contextlib import suppress

from ..errors import SourceError


class WorkerFailure(SourceError):
    """The worker failed or could not finish normal teardown."""


class CleanupUnconfirmed(WorkerFailure):
    cleanup_unconfirmed = True


class WindowsJob:
    def __init__(self, pid):
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
                raise OSError(ctypes.get_last_error(), 'could not assign owned worker job')
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


def _child(connection, function):
    if os.name != 'nt':
        os.setsid()
    connection.send(('ready', None))
    arguments = connection.recv()
    try:
        result = function(connection, arguments)
        connection.send(('result', result))
    except BaseException as error:  # noqa: BLE001 - relay worker termination without secrets
        connection.send(('error', type(error).__name__))
    finally:
        connection.close()


def run_worker(
    function,
    arguments,
    *,
    interactive=False,
    pause=None,
    check=None,
    timeout: float | None = 60,
    grace: float = 5,
):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_child, args=(child, function))
    process.start()
    child.close()
    job = None
    deadline = None if timeout is None else time.monotonic() + timeout
    result = None
    try:
        if not parent.poll(10) or parent.recv()[0] != 'ready':
            raise WorkerFailure('browser worker startup failed')
        if os.name == 'nt':
            job = WindowsJob(process.pid)
        parent.send(arguments)
        while True:
            if check:
                check()
            if deadline is not None and time.monotonic() >= deadline:
                raise WorkerFailure('browser worker deadline exceeded')
            if parent.poll(0.1):
                kind, value = parent.recv()
                if kind == 'pause':
                    if pause is None or not interactive:
                        raise WorkerFailure('unexpected interactive worker request')
                    pause(value)
                    parent.send(None)
                    deadline = None if timeout is None else time.monotonic() + timeout
                elif kind == 'closing':
                    deadline = time.monotonic() + grace
                elif kind == 'error':
                    raise WorkerFailure('browser worker failed: ' + value)
                elif kind == 'result':
                    result = value
                    break
            elif not process.is_alive():
                raise WorkerFailure('browser worker exited without a result')
        process.join(grace)
        if process.is_alive():
            raise WorkerFailure('browser worker teardown exceeded deadline')
        return result
    finally:
        if process.is_alive():
            if job:
                job.terminate()
            elif os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.terminate()
            process.join(5)
        # Kill any descendants left behind even after normal worker exit.
        if job:
            job.terminate()
            job.close()
        elif os.name != 'nt':
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        parent.close()
        if process.is_alive():
            raise CleanupUnconfirmed('browser cleanup could not be confirmed')
        process.close()
