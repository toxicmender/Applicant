"""applicant.db: the record of every job seen, application made and rating read.

One SQLite file, beside the JSON and CSV it serves. The database is the system
of record; `job_listing.json` and `applied_jobs.csv` are its exports, rewritten
after every change so a spreadsheet import or a quick look in an editor still
works exactly as before.

Each export is a *collection* here, named by its path relative to the database,
so two listings side by side stay two listings. The store remembers a digest of
every file it wrote. When a file no longer matches - edited by hand, replaced,
deleted, or never seen before - the file wins and is imported afresh. That one
rule is both the migration (a working directory from before the database is
imported on first use) and the guarantee that the file you see is the data the
next run uses.

Why SQLite: every write is a transaction across tables, so an application and
the dedupe record behind it land together or not at all; lookups for dedupe
are indexed rather than a re-read of the whole CSV; history - ratings over
time - has somewhere to live. It is in the standard library, works offline and
is one file to back up. See docs/architecture-plan.md, decision D8.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.dedupe import fingerprint, key
from ...domain.job import Job
from ...errors import StoreError
from ...files import read_document, write_document
from ...storage import APPLIED_COLUMNS, ApplicationLog, plan_rows, write_rows

logger = logging.getLogger(__name__)

DB_NAME = 'applicant.db'

# Each entry moves the schema one version on; PRAGMA user_version records how
# far a database has come. Never edit a shipped entry - append a new one.
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        """CREATE TABLE documents (
            name TEXT PRIMARY KEY,   -- the export's path, relative to the database
            digest TEXT              -- sha256 of the bytes last written or imported
        )""",
        """CREATE TABLE jobs (
            listing TEXT NOT NULL,
            key TEXT NOT NULL,       -- identity within a board, see domain.dedupe.key
            seq INTEGER NOT NULL,    -- export order: first seen first
            fingerprint TEXT,        -- identity across boards
            payload TEXT NOT NULL,
            PRIMARY KEY (listing, key)
        )""",
        'CREATE INDEX jobs_order ON jobs (listing, seq)',
        'CREATE INDEX jobs_fingerprint ON jobs (listing, fingerprint)',
        """CREATE TABLE applications (
            log TEXT NOT NULL,
            seq INTEGER NOT NULL,
            source TEXT,
            source_id TEXT,          -- as the CSV holds it: '' when there was none
            fingerprint TEXT,
            status TEXT,
            row TEXT NOT NULL,
            PRIMARY KEY (log, seq)
        )""",
        'CREATE INDEX applications_key ON applications (log, source, source_id)',
        'CREATE INDEX applications_fingerprint ON applications (log, fingerprint)',
        """CREATE TABLE ratings (
            company TEXT NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )""",
        'CREATE INDEX ratings_history ON ratings (company, source, fetched_at)',
    ),
)


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    """One applicant.db. Open it with `Store.beside(path)` or `Store(db_path)`."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.root = self.path.resolve().parent
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # created owner-only before SQLite opens it: it holds everything the
            # listing and the application log do (ASVS 14.2)
            os.close(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600))
            self.db = sqlite3.connect(self.path)
            self._migrate()
        except (OSError, sqlite3.Error) as error:
            raise StoreError(f'could not open {self.path}: {error}') from error

    @classmethod
    def beside(cls, file: str | os.PathLike[str]) -> Store:
        """The store for a file: applicant.db in the same directory."""
        return cls(Path(file).resolve().parent / DB_NAME)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            with self.db:
                yield self.db
        except sqlite3.Error as error:
            raise StoreError(f'{self.path}: {error}') from error

    def _migrate(self) -> None:
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        for number, statements in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.db:
                for statement in statements:
                    self.db.execute(statement)
                self.db.execute(f'PRAGMA user_version = {number}')
            logger.debug(f'{self.path}: schema at version {number}')

    # -- which file is which collection -----------------------------------

    def name(self, file: str | os.PathLike[str]) -> str:
        """A file's collection name: relative to the database where it can be,
        so moving the whole directory keeps every collection."""
        resolved = Path(file).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def _known_digest(self, name: str) -> str | None:
        row = self.db.execute('SELECT digest FROM documents WHERE name = ?', (name,)).fetchone()
        return row[0] if row else None

    def _remember(self, db: sqlite3.Connection, name: str, file: Path) -> None:
        db.execute(
            'INSERT INTO documents (name, digest) VALUES (?, ?) '
            'ON CONFLICT (name) DO UPDATE SET digest = excluded.digest',
            (name, _digest(file)),
        )

    def _stale(self, name: str, file: Path, table: str, column: str) -> bool:
        """Whether the file says something the database does not."""
        digest = _digest(file)
        if digest is not None:
            return digest != self._known_digest(name)
        # gone: stale only if we hold rows it used to have
        return (
            self.db.execute(f'SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1', (name,)).fetchone()
            is not None
        )

    # -- the job listing --------------------------------------------------

    def _sync_listing(self, file: Path, quarantine: bool) -> str | None:
        """Bring the collection in line with the file, if the file moved on.

        -> the collection name, or None when the file is unreadable and was
        left in place (a read that must not destroy it): the caller then sees
        what `load_jobs` would, an empty listing.
        """
        name = self.name(file)
        if not self._stale(name, file, 'jobs', 'listing'):
            return name

        existed = file.exists()
        document = read_document(file, quarantine=quarantine)
        if not quarantine and existed and not document and not _is_empty_object(file):
            return None  # unreadable, kept; read_document has said so

        items: dict[str, dict] = {}
        for item in _listed(document):
            items[_key(item)] = item
        with self._transaction() as db:
            db.execute('DELETE FROM jobs WHERE listing = ?', (name,))
            db.executemany(
                'INSERT INTO jobs (listing, key, seq, fingerprint, payload) VALUES (?, ?, ?, ?, ?)',
                [
                    (name, item_key, seq, fingerprint(item), json.dumps(item, ensure_ascii=False))
                    for seq, (item_key, item) in enumerate(items.items())
                ],
            )
            self._remember(db, name, file)
        if existed:
            logger.info(f'{file}: imported {len(items)} job(s) into {self.path}')
        return name

    def job_items(self, file: str | os.PathLike[str]) -> list[dict]:
        """Every stored record of a listing, in the order it was first seen."""
        name = self._sync_listing(Path(file), quarantine=False)
        if name is None:
            return []
        rows = self.db.execute(
            'SELECT payload FROM jobs WHERE listing = ? ORDER BY seq', (name,)
        ).fetchall()
        return [json.loads(payload) for (payload,) in rows]

    def save_jobs(self, jobs: Iterable[Job], file: str | os.PathLike[str]) -> int:
        """Merge into a listing, newest write winning; export it. -> total stored.

        The same merge as `storage.save_jobs`: a job already held is updated in
        place, a new one goes on the end.
        """
        target = Path(file)
        name = self._sync_listing(target, quarantine=True)
        assert name is not None  # quarantine moved anything unreadable aside
        with self._transaction() as db:
            before = self._count(db, name)
            (last,) = db.execute(
                'SELECT COALESCE(MAX(seq), -1) FROM jobs WHERE listing = ?', (name,)
            ).fetchone()
            for job in jobs:
                item = job.to_dict()
                last += 1
                db.execute(
                    'INSERT INTO jobs (listing, key, seq, fingerprint, payload) '
                    'VALUES (?, ?, ?, ?, ?) ON CONFLICT (listing, key) DO UPDATE SET '
                    'fingerprint = excluded.fingerprint, payload = excluded.payload',
                    (
                        name,
                        _key(item),
                        last,
                        fingerprint(item),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
            total = self._count(db, name)
            items = [
                json.loads(payload)
                for (payload,) in db.execute(
                    'SELECT payload FROM jobs WHERE listing = ? ORDER BY seq', (name,)
                )
            ]
            # the export is part of the transaction: if it cannot be written,
            # the database does not move on without it
            write_document(target, {'list': items})
            self._remember(db, name, target)
        logger.info(f'{target}: {total - before} new job(s), {total} stored')
        return total

    def _count(self, db: sqlite3.Connection, name: str) -> int:
        return db.execute('SELECT COUNT(*) FROM jobs WHERE listing = ?', (name,)).fetchone()[0]

    # -- the application log ----------------------------------------------

    def _sync_log(self, file: Path) -> str:
        name = self.name(file)
        if not self._stale(name, file, 'applications', 'log'):
            return name
        rows = ApplicationLog(str(file)).rows()
        with self._transaction() as db:
            db.execute('DELETE FROM applications WHERE log = ?', (name,))
            db.executemany(
                'INSERT INTO applications (log, seq, source, source_id, fingerprint, status, row) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                [_application(name, seq, row) for seq, row in enumerate(rows)],
            )
            self._remember(db, name, file)
        if rows:
            logger.info(f'{file}: imported {len(rows)} application(s) into {self.path}')
        return name

    def application_rows(self, file: str | os.PathLike[str]) -> list[dict]:
        name = self._sync_log(Path(file))
        return [
            json.loads(row)
            for (row,) in self.db.execute(
                'SELECT row FROM applications WHERE log = ? ORDER BY seq', (name,)
            )
        ]

    def application_counts(self, file: str | os.PathLike[str]) -> dict[str, int]:
        """status -> rows holding it; an empty status counts as 'unknown'."""
        name = self._sync_log(Path(file))
        return dict(
            self.db.execute(
                "SELECT COALESCE(NULLIF(status, ''), 'unknown'), COUNT(*) FROM applications "
                'WHERE log = ? GROUP BY 1',
                (name,),
            ).fetchall()
        )

    def record_applications(
        self, entries: Iterable[tuple[Job, str, str]], file: str | os.PathLike[str]
    ) -> int:
        """Log what is new among `entries`, by the same rules as the CSV; export.

        The dedupe questions are answered from the indexes, not a re-read.
        """
        target = Path(file)
        name = self._sync_log(target)
        reader = self.db

        def seen(source: str | None, job_id: str | None) -> bool:
            # the CSV holds a missing id as '', and a job's missing id is None:
            # as in the file backend, the two never match
            return (
                reader.execute(
                    'SELECT 1 FROM applications WHERE log = ? AND source IS ? AND source_id IS ? '
                    'LIMIT 1',
                    (name, source, job_id),
                ).fetchone()
                is not None
            )

        def boards_for(mark: str) -> set[str | None]:
            return {
                source
                for (source,) in reader.execute(
                    'SELECT DISTINCT source FROM applications WHERE log = ? AND fingerprint = ?',
                    (name, mark),
                )
            }

        fresh = plan_rows(entries, seen=seen, boards_for=boards_for)
        if not fresh:
            logger.debug(f'{target}: nothing new to record')
            return 0

        with self._transaction() as db:
            (last,) = db.execute(
                'SELECT COALESCE(MAX(seq), -1) FROM applications WHERE log = ?', (name,)
            ).fetchone()
            db.executemany(
                'INSERT INTO applications (log, seq, source, source_id, fingerprint, status, row) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                [_application(name, last + 1 + index, row) for index, row in enumerate(fresh)],
            )
            rows = [
                json.loads(row)
                for (row,) in db.execute(
                    'SELECT row FROM applications WHERE log = ? ORDER BY seq', (name,)
                )
            ]
            _export_log(target, rows)
            self._remember(db, name, target)
        logger.info(f'{target}: recorded {len(fresh)} application(s)')
        return len(fresh)

    # -- ratings, kept over time ------------------------------------------

    def add_ratings(self, company: str, ratings: Iterable[tuple[str, dict]]) -> None:
        """Record each (source, rating) as read now; nothing is overwritten."""
        fetched_at = _now()
        with self._transaction() as db:
            db.executemany(
                'INSERT INTO ratings (company, source, fetched_at, payload) VALUES (?, ?, ?, ?)',
                [
                    (company.lower(), source, fetched_at, json.dumps(rating, ensure_ascii=False))
                    for source, rating in ratings
                ],
            )

    def rating_history(self, company: str, source: str | None = None) -> list[dict]:
        """Every rating read for a company, oldest first, each with `fetched_at`."""
        query = 'SELECT source, fetched_at, payload FROM ratings WHERE company = ?'
        params: list[Any] = [company.lower()]
        if source is not None:
            query += ' AND source = ?'
            params.append(source)
        return [
            {**json.loads(payload), 'source': found, 'fetched_at': fetched_at}
            for found, fetched_at, payload in self.db.execute(
                query + ' ORDER BY fetched_at, rowid', params
            )
        ]


def _is_empty_object(file: Path) -> bool:
    """A listing that is valid JSON and simply empty, rather than unreadable."""
    try:
        return json.loads(file.read_text(encoding='utf-8')) == {}
    except (OSError, ValueError):
        return False


def _key(item: dict) -> str:
    return json.dumps(list(key(item)), ensure_ascii=False)


def _listed(document: dict) -> list[dict]:
    items = document.get('list', [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _application(name: str, seq: int, row: dict) -> tuple:
    source_id = row.get('id')
    return (
        name,
        seq,
        row.get('source'),
        '' if source_id is None else str(source_id),
        fingerprint(row),
        row.get('status'),
        json.dumps(row, ensure_ascii=False),
    )


def _export_log(target: Path, rows: list[dict]) -> None:
    """The whole log as a CSV, written the way ApplicationLog appends it - so the
    bytes match - but all at once, to a temporary file, then swapped in."""
    temporary = target.with_name(f'.{target.name}.tmp')
    try:
        with open(temporary, 'w', encoding='utf-8-sig', newline='') as handle:
            write_rows(handle, rows, header=True)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            os.chmod(temporary, target.stat().st_mode & 0o777)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


__all__ = ['APPLIED_COLUMNS', 'DB_NAME', 'MIGRATIONS', 'Store']
