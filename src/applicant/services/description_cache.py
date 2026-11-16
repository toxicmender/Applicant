"""Bounded local description evidence for interrupted search enrichment."""

import json
import logging
import sqlite3
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path

from ..domain.canonical import canonical_json, content_hash
from ..domain.dedupe import key
from ..financials.tracker import _utc_timestamp
from ..infra.store.financials_lock import export_lock
from ..permissions import make_private
from .company_cache import CacheClock

logger = logging.getLogger(__name__)


class DescriptionCache:
    max_entries = 5000
    max_bytes = 50 * 1024 * 1024

    def __init__(self, listing, *, clock=None):
        path = Path(listing)
        self.path = path.with_name(f'.{path.name}.descriptions.sqlite')
        self.clock = clock or CacheClock()
        self.anchor = self.clock.wall(), self.clock.monotonic()

    @contextmanager
    def _database(self):
        with export_lock(self.path), closing(sqlite3.connect(self.path)) as db, db:
            make_private(self.path)
            db.execute(
                'CREATE TABLE IF NOT EXISTS descriptions '
                '(id TEXT PRIMARY KEY, payload BLOB, size INTEGER, used TEXT)'
            )
            db.execute('CREATE INDEX IF NOT EXISTS description_lru ON descriptions(used, id)')
            db.execute(
                'CREATE TABLE IF NOT EXISTS metadata '
                '(id INTEGER PRIMARY KEY CHECK (id=1), entries INTEGER, bytes INTEGER, wall TEXT)'
            )
            db.execute('INSERT OR IGNORE INTO metadata VALUES (1,0,0,NULL)')
            yield db

    def _time(self, db):
        wall = self.clock.wall()
        start, tick = self.anchor
        estimated = start + timedelta(seconds=self.clock.monotonic() - tick)
        (value,) = db.execute('SELECT wall FROM metadata WHERE id=1').fetchone()
        watermark = _utc_timestamp(value)
        if value is not None and (watermark is None or watermark > wall + timedelta(minutes=15)):
            logger.warning('description cache clock is more than 15 minutes behind its watermark')
            watermark = None
        reference = max(estimated, watermark) if watermark is not None else estimated
        trusted = wall <= reference + timedelta(minutes=2)
        if not trusted:
            logger.warning('ignoring a forward clock jump for description cache freshness')
            return reference, False, watermark
        current = max(wall, reference) if wall >= reference - timedelta(minutes=15) else wall
        return current, True, watermark

    def read(self, job):
        if not self.path.is_file():
            return False, None
        identifier = content_hash(key(job.to_dict()))
        with self._database() as db:
            row = db.execute(
                'SELECT payload FROM descriptions WHERE id=?', (identifier,)
            ).fetchone()
            if row is None:
                return False, None
            evidence = json.loads(row[0])
            stamp = _utc_timestamp(evidence['at'])
            current, _, _ = self._time(db)
            timeout = timedelta(days=30 if evidence['description'] else 7)
            if stamp is None or not -timedelta(minutes=2) <= current - stamp < timeout:
                return False, None
            if any(
                evidence['card'].get(field) not in (None, value)
                for field, value in visible_card(job).items()
            ):
                return False, None
            db.execute(
                'UPDATE descriptions SET used=? WHERE id=?', (current.isoformat(), identifier)
            )
            return True, evidence['description']

    def write(self, job, description):
        identifier = content_hash(key(job.to_dict()))
        with self._database() as db:
            current, trusted, watermark = self._time(db)
            if not trusted:
                return
            sample = self.clock.wall()
            stamp = sample.isoformat()
            payload = canonical_json(
                {'description': description, 'at': stamp, 'card': visible_card(job)}
            ).encode()
            if len(payload) > self.max_bytes:
                return
            (entries, total) = db.execute(
                'SELECT entries, bytes FROM metadata WHERE id=1'
            ).fetchone()
            previous = db.execute(
                'SELECT size FROM descriptions WHERE id=?', (identifier,)
            ).fetchone()
            entries += previous is None
            total += len(payload) - (previous[0] if previous else 0)
            db.execute(
                'INSERT OR REPLACE INTO descriptions VALUES (?,?,?,?)',
                (identifier, payload, len(payload), current.isoformat()),
            )
            while entries > self.max_entries or total > self.max_bytes:
                row = db.execute(
                    'SELECT id, size FROM descriptions ORDER BY used, id LIMIT 1'
                ).fetchone()
                db.execute('DELETE FROM descriptions WHERE id=?', (row[0],))
                entries -= 1
                total -= row[1]
            db.execute(
                'UPDATE metadata SET entries=?,bytes=?,wall=? WHERE id=1',
                (entries, total, max(sample, watermark or sample).isoformat()),
            )


def visible_card(job):
    fields = (
        'title',
        'company',
        'location',
        'posted',
        'experience_text',
        'employment_type',
        'remote',
    )
    return {field: value for field in fields if (value := getattr(job, field)) is not None}
