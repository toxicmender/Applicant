"""Nonblocking, non-reentrant file locks owned by the current process."""

import errno
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

from ..permissions import make_private

_registry_lock = Lock()
_held: set[str] = set()
_retained_handles = []


@contextmanager
def advisory_lock(path, busy, *, retain_on_cleanup_failure=False):
    path = Path(path).expanduser().resolve()
    identity = os.path.normcase(str(path))
    with _registry_lock:
        if identity in _held:
            raise busy()
        _held.add(identity)
    handle = None
    retain = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = path.open('a+b')
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open('a+b')
        os.set_inheritable(handle.fileno(), False)
        make_private(path)
        try:
            if os.name == 'nt':
                import msvcrt

                # Windows allows locking bytes past EOF. No initial write is needed.
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise busy() from None
            raise
        try:
            yield
        finally:
            retain = retain_on_cleanup_failure and bool(
                getattr(sys.exc_info()[1], 'cleanup_unconfirmed', False)
            )
            if retain:
                _retained_handles.append(handle)
    finally:
        if not retain:
            try:
                if handle is not None:
                    handle.close()
            finally:
                with _registry_lock:
                    _held.remove(identity)
