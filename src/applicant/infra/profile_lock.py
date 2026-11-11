"""OS-owned advisory locks: a killed process cannot leave a stale lease."""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

from ..errors import ProfileBusy
from ..permissions import make_private

_retained_handles = []


@contextmanager
def profile_lock(path: Path):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    retain = False
    try:
        make_private(path)
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProfileBusy(
                'profile_busy: provider profile is in use; retry after the other process exits'
            ) from None
        try:
            yield
        finally:
            retain = bool(getattr(sys.exc_info()[1], 'cleanup_unconfirmed', False))
            if retain:
                _retained_handles.append(handle)
            else:
                _unlock(handle)
    finally:
        if not retain:
            handle.close()


def _unlock(handle):
    handle.seek(0)
    if os.name == 'nt':
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
