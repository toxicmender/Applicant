"""Durable publication of SQLite-managed file exports."""

import errno
import hashlib
import os
import time

from ...errors import ExportConflict, ExportPending
from ...files import write_document_bytes


def read_bytes(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def sync_parent(target):
    if os.name != 'nt':
        fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_replace(temporary, target):
    if os.name == 'nt':
        import ctypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        move = kernel.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(os.path.abspath(temporary), os.path.abspath(target), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        os.replace(temporary, target)
        sync_parent(target)


def reconcile(
    target, pending, acknowledge, *, merge=None, publish=write_document_bytes, sleep=time.sleep
):
    if pending is None:
        return
    baseline, baseline_digest, intended, intended_digest = pending
    for delay in (0.05, 0.1, 0.2, 0.4, None):
        current = read_bytes(target)
        current_digest = digest(current)
        if current_digest != intended_digest:
            if current is not None and current_digest != baseline_digest:
                if merge is None:
                    raise ExportConflict(
                        f'{target}: file and SQLite changed; use store resolve-export'
                    )
                intended, intended_digest = merge(baseline, current, intended)
                baseline, baseline_digest = current, current_digest
            try:
                publish(target, intended, replace=durable_replace)
            except OSError as error:
                code = getattr(error, 'winerror', None)
                if error.errno == errno.ENOSPC or code == 112:
                    raise
                if code == 5 or (code in (32, 33) and delay is None):
                    raise ExportPending(
                        f'{target}: close applications using the file or check permissions and rerun'
                    ) from error
                if code not in (32, 33) or delay is None:
                    raise
                sleep(delay)
                continue
        sync_parent(target)
        acknowledge(intended_digest)
        return
