"""One owner per financials export, shared by the file and SQLite backends."""

import errno
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ...errors import DatabaseBusy, FinancialsBusy, StorageFull, StoreError, sqlite_busy
from ..advisory_lock import advisory_lock


@contextmanager
def financials_lock(file):
    target = Path(file).resolve()
    try:
        with advisory_lock(
            target.with_name(f'.{target.name}.lock'),
            lambda: FinancialsBusy(f'{target}: financials history is in use; rerun the command'),
        ):
            yield
    except (OSError, sqlite3.Error, StoreError) as error:
        cause = error.__cause__ if isinstance(error, StoreError) else error
        if isinstance(cause, OSError) and (
            cause.errno == errno.ENOSPC or getattr(cause, 'winerror', None) == 112
        ):
            raise StorageFull(f'{cause.filename or target}: free disk space and rerun') from error
        if isinstance(cause, sqlite3.Error) and (
            getattr(cause, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL
        ):
            raise StorageFull(f'{target.parent}: free disk space and rerun') from error
        if isinstance(error, StoreError):
            raise
        if sqlite_busy(cause):
            raise DatabaseBusy(f'{target}: database is busy; rerun the command') from error
        raise StoreError(f'{target}: {error}') from error
