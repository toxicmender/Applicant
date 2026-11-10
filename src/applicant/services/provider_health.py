"""Short SQLite transactions lease acquisition; no network work holds a writer."""

from datetime import datetime, timedelta
from uuid import uuid4

from ..domain.contracts import now
from ..domain.providers import provider
from ..infra.store.sqlite import Store


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
