"""Nonblocking, non-reentrant file locks owned by the current process."""

import errno
import os
import stat
import sys
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from threading import Lock

from ..permissions import make_private
from .path_identity import lock_identity, physical_path

_registry_lock = Lock()
_held: set[str] = set()
_retained_handles = []


@contextmanager
def _parent(path):
    if os.name == 'nt':
        from .windows_paths import directory_handle

        with directory_handle(path, create=True) as handle:
            yield handle
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
    if opened.st_nlink == 0:
        raise FileNotFoundError(errno.ENOENT, 'lock leaf was removed during acquisition', path)
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
def advisory_lock(path, busy, *, retain_on_cleanup_failure=False, retry_delays=()):
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
                    if os.name == 'nt':
                        from .windows_paths import lock_descriptor

                        descriptor = lock_descriptor(parent, path)
                    else:
                        descriptor = os.open(
                            path.name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=parent
                        )
                    os.set_inheritable(descriptor, False)
                    relative = None if os.name == 'nt' else parent
                    _verify_leaf(descriptor, path, relative)
                    for delay in (*retry_delays, None):
                        try:
                            _take_lock(descriptor)
                        except OSError as error:
                            if error.errno not in (
                                errno.EACCES,
                                errno.EAGAIN,
                                errno.EWOULDBLOCK,
                                errno.EDEADLK,
                            ):
                                raise
                            if delay is None:
                                raise busy() from None
                            time.sleep(delay)
                        else:
                            break
                    _verify_leaf(descriptor, path, relative)
                    if os.name == 'nt':
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
