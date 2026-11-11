"""Bounded SQLite maintenance which never creates or upgrades a database."""

import sqlite3
from pathlib import Path

from ...errors import StoreError


def configure_connection(db, *, writable=True):
    db.execute('PRAGMA busy_timeout = 5000')
    db.execute('PRAGMA foreign_keys = ON')
    if writable:
        db.execute('PRAGMA recursive_triggers = ON')
        db.execute('PRAGMA wal_autocheckpoint = 1000')


def checkpoint(path: Path, mode='passive'):
    if mode not in ('passive', 'truncate'):
        raise ValueError('checkpoint mode must be passive or truncate')
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True, timeout=5)
        try:
            configure_connection(db)
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                return {'mode': mode, 'status': 'not_applicable'}
            busy, frames, completed = db.execute(
                f'PRAGMA wal_checkpoint({mode.upper()})'
            ).fetchone()
            remaining = max(0, frames - completed) if frames >= 0 and completed >= 0 else None
            status = 'busy' if busy else 'partial' if remaining else 'complete'
            wal = Path(str(path) + '-wal')
            return {
                'mode': mode,
                'status': status,
                'busy': busy,
                'log_frames': frames,
                'checkpointed_frames': completed,
                'remaining_frames': remaining,
                'wal_bytes': wal.stat().st_size if wal.exists() else 0,
                'retryable': bool(busy or remaining),
                'note': 'Active readers or writers may prevent completion; retry after they exit.'
                if busy or remaining
                else '',
            }
        finally:
            db.close()
    except sqlite3.OperationalError as error:
        raise StoreError(f'checkpoint unavailable (database may be busy): {error}') from error
