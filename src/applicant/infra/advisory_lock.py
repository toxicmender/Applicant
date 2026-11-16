"""Nonblocking, non-reentrant file locks owned by the current process."""

import errno
import os
import stat
import sys
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Lock

from ..permissions import make_private
from .path_identity import lock_identity, physical_path

_registry_lock = Lock()
_held: set[str] = set()
_retained_handles = []


def _windows_open(path):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    # Open the reparse point itself and omit delete sharing for the lock's lifetime.
    handle = create(str(path), 0xC0000000, 3, None, 4, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close(handle)
        raise
    return descriptor


@contextmanager
def _parent(path):
    if os.name == 'nt':
        path.mkdir(parents=True, exist_ok=True)
        yield None
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=current)
                child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        yield current
    finally:
        os.close(current)


def _verify_leaf(descriptor, path, parent):
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
        raise OSError(errno.EINVAL, 'lock must be a regular file with one link', path)
    current = os.stat(
        path.name if parent is not None else path, dir_fd=parent, follow_symlinks=False
    )
    if getattr(current, 'st_file_attributes', 0) & 0x400:
        raise OSError(errno.EINVAL, 'lock must not be a reparse point', path)
    if not os.path.samestat(opened, current):
        raise FileNotFoundError(errno.ENOENT, 'lock leaf changed during acquisition', path)


def _take_lock(descriptor):
    if os.name == 'nt':
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def advisory_lock(path, busy, *, retain_on_cleanup_failure=False):
    requested = Path(path).expanduser()
    path = physical_path(requested.parent) / requested.name
    identity = lock_identity(path)
    with _registry_lock:
        if identity in _held:
            raise busy()
        _held.add(identity)
    descriptor = None
    retain = False
    try:
        for attempt in range(2):
            try:
                with _parent(path.parent) as parent:
                    if parent is None:
                        descriptor = _windows_open(path)
                    else:
                        descriptor = os.open(
                            path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent
                        )
                    os.set_inheritable(descriptor, False)
                    _verify_leaf(descriptor, path, parent)
                    try:
                        _take_lock(descriptor)
                    except OSError as error:
                        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise busy() from None
                        raise
                    _verify_leaf(descriptor, path, parent)
                    if parent is None:
                        make_private(path)
                    else:
                        os.fchmod(descriptor, 0o600)
            except FileNotFoundError:
                if attempt:
                    raise
                if descriptor is not None:
                    os.close(descriptor)
                    descriptor = None
            else:
                break
        try:
            yield
        finally:
            retain = retain_on_cleanup_failure and bool(
                getattr(sys.exc_info()[1], 'cleanup_unconfirmed', False)
            )
            if retain:
                _retained_handles.append(descriptor)
    finally:
        if not retain:
            try:
                if descriptor is not None:
                    os.close(descriptor)
            finally:
                with _registry_lock:
                    _held.remove(identity)
