"""Short SQLite transactions lease acquisition; no network work holds a writer."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from ..domain.contracts import now
from ..domain.providers import provider
from ..errors import DatabaseBusy, sqlite_busy
from ..infra.store.maintenance import configure_connection
from ..infra.store.sqlite import MIGRATIONS, Store


def health(database, name):
    provider(name)
    with Store.open_readonly(database) as store:
        row = store.db.execute(
            'SELECT status,failures,eligible_at,lease_until,reason FROM provider_health WHERE provider=?',
            (name,),
        ).fetchone()
    return (
        dict(zip(('status', 'failures', 'eligible_at', 'lease_until', 'reason'), row, strict=True))
        if row
        else {'status': 'ready', 'failures': 0}
    )


def recover_authentication(database, name):
    """Inspect and update existing health only; never initialize or migrate."""
    path = Path(database)
    if not path.exists():
        return {'status': 'skipped_missing_database'}
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=rw', uri=True)
    try:
        configure_connection(db)
        if db.execute('PRAGMA user_version').fetchone()[0] != len(MIGRATIONS):
            return {
                'status': 'schema_incompatible',
                'retryable': False,
                'command': f'applicant db upgrade --database "{path}"',
            }
        with db:
            changed = db.execute(
                "UPDATE provider_health SET status='ready',failures=0,reason=NULL "
                "WHERE provider=? AND status='needs_setup' AND lease_token IS NULL",
                (provider(name).id,),
            ).rowcount
        return {'status': 'recovered' if changed else 'unchanged'}
    except sqlite3.Error as error:
        return {
            'status': 'database_busy' if sqlite_busy(error) else 'database_error',
            'retryable': sqlite_busy(error),
        }
    finally:
        db.close()


def acquire(database, name, *, clock=now, allow_degraded=False):
    provider(name)
    current = clock()
    with Store(database) as store:
        store.db.execute('BEGIN IMMEDIATE')
        try:
            row = store.db.execute(
                'SELECT status,eligible_at,lease_until FROM provider_health WHERE provider=?',
                (name,),
            ).fetchone()
            if row and (
                (row[0] in ('needs_setup', 'adapter_degraded') and not allow_degraded)
                or (row[1] and datetime.fromisoformat(row[1]) > current)
                or (row[2] and datetime.fromisoformat(row[2]) > current)
            ):
                store.db.rollback()
                return {'status': row[0] if row[0] != 'ready' else 'leased', 'eligible_at': row[1]}
            token = str(uuid4())
            store.db.execute(
                "INSERT INTO provider_health(provider,status,lease_until,lease_token) VALUES (?,'ready',?,?) ON CONFLICT(provider) DO UPDATE SET lease_until=excluded.lease_until,lease_token=excluded.lease_token",
                (name, (current + timedelta(minutes=10)).isoformat(), token),
            )
            store.db.commit()
            return {'status': 'acquired', 'token': token}
        except BaseException:
            store.db.rollback()
            raise


def finish(database, name, token, outcome, *, retry_at=None, clock=now):
    current = clock()
    with Store(database) as store:
        store.db.execute('BEGIN IMMEDIATE')
        try:
            row = store.db.execute(
                'SELECT failures,lease_token FROM provider_health WHERE provider=?', (name,)
            ).fetchone()
            if not row or row[1] != token:
                raise ValueError('provider acquisition lease no longer belongs to this operation')
            failures = 0 if outcome == 'success' else row[0] + 1
            status, eligible = 'ready', None
            if outcome in ('needs_setup', 'adapter_degraded'):
                status = outcome
            elif outcome in ('blocked', 'throttled') or (
                outcome == 'unavailable' and failures >= 3
            ):
                status = 'cooldown'
                seconds = {'blocked': 86400, 'throttled': 3600, 'unavailable': 900}[outcome]
                eligible = current + timedelta(seconds=seconds)
                if retry_at is not None:
                    eligible = max(eligible, retry_at)
            store.db.execute(
                'UPDATE provider_health SET status=?,failures=?,eligible_at=?,lease_until=NULL,lease_token=NULL,reason=? WHERE provider=?',
                (status, failures, eligible.isoformat() if eligible else None, outcome, name),
            )
            store.db.commit()
        except BaseException:
            store.db.rollback()
            raise
    return {
        'status': status,
        'failures': failures,
        'eligible_at': eligible.isoformat() if eligible else None,
    }


def renew(database, name, token, *, clock=now):
    with Store(database) as store, store.db:
        result = store.db.execute(
            'UPDATE provider_health SET lease_until=? WHERE provider=? AND lease_token=?',
            ((clock() + timedelta(minutes=10)).isoformat(), name, token),
        )
        if result.rowcount != 1:
            raise ValueError('provider acquisition lease ownership lost')


def abandon(database, name, token):
    with Store(database) as store, store.db:
        store.db.execute(
            'UPDATE provider_health SET lease_until=NULL,lease_token=NULL WHERE provider=? AND lease_token=?',
            (name, token),
        )


class LeaseFailure(RuntimeError):
    """Infrastructure failure, never a provider health failure."""

    retryable = True


class Acquisition:
    """Own a lease and a bounded renewal worker through every exit path."""

    def __init__(self, database, name, *, clock=now, probe=False, interval=60):
        self.database, self.name, self.clock = database, name, clock
        self.probe, self.interval = probe, interval
        self.stop = Event()
        self.error = None
        self.thread = None
        self.token = None

    def __enter__(self):
        self.result = acquire(self.database, self.name, clock=self.clock, allow_degraded=self.probe)
        if self.result['status'] == 'acquired':
            self.token = self.result['token']
            self.thread = Thread(target=self._run, daemon=True)
            self.thread.start()
        return self

    def tick(self):
        for attempt in range(2):
            try:
                renew(self.database, self.name, self.token, clock=self.clock)
                return
            except Exception as error:
                if attempt == 0 and (isinstance(error, DatabaseBusy) or sqlite_busy(error)):
                    if self.stop.wait(1):
                        return
                else:
                    raise

    def _run(self):
        try:
            while not self.stop.wait(self.interval):
                self.tick()
        except BaseException as error:  # noqa: BLE001 - the owner must observe worker failure
            self.error = error

    def check(self):
        if self.error is not None:
            raise LeaseFailure(
                'acquisition renewal failed: ' + type(self.error).__name__
            ) from self.error

    def shutdown(self):
        self.stop.set()
        if self.thread:
            self.thread.join(12)
            if self.thread.is_alive():
                raise LeaseFailure('acquisition renewal did not stop within 12 seconds')

    def finish(self, outcome, **kwargs):
        self.shutdown()
        self.check()
        result = finish(self.database, self.name, self.token, outcome, clock=self.clock, **kwargs)
        self.token = None
        return result

    def __exit__(self, *_):
        try:
            self.shutdown()
        finally:
            if self.token:
                abandon(self.database, self.name, self.token)
