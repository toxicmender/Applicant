"""applicant.db: the record of every job seen, application made and rating read.

One SQLite file, beside the JSON and CSV it serves. The database is the system
of record; `job_listing.json` and `applied_jobs.csv` are its exports, rewritten
after every change so a spreadsheet import or a quick look in an editor still
works exactly as before.

Each export is a *collection* here, named by its path relative to the database,
so two listings side by side stay two listings. The store remembers a digest of
every file it wrote. When a file no longer matches - edited by hand, replaced,
deleted, or never seen before - the file wins and is imported afresh. (Not a
file that cannot be read: that one is moved aside, and the database, the only
good copy left, writes the file again.) That one rule is both the migration (a working directory from before the database is
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
import time
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...domain.dedupe import fingerprint, key
from ...domain.job import Job
from ...errors import DatabaseBusy, ExportConflict, StoreError, sqlite_busy
from ...files import (
    decode_document,
    document_bytes,
    read_document,
    write_document_bytes,
)
from ...permissions import make_private
from ...storage import APPLIED_COLUMNS, PROVISIONAL, plan_rows
from .application_csv import ApplicationCSV
from .benchmarks import BENCHMARK_MIGRATION
from .company_evidence import COMPANY_EVIDENCE_MIGRATION
from .feedback import FEEDBACK_MIGRATION
from .financials_lock import financials_lock
from .financials_merge import merge_financial_exports
from .maintenance import configure_connection
from .personalization import PERSONALIZATION_MIGRATION
from .publication import digest as _bytes_digest
from .publication import read_bytes as _financials_bytes
from .publication import reconcile, single_attempt
from .ranking_schema import RANKING_MIGRATION
from .research import RESEARCH_MIGRATION

logger = logging.getLogger(__name__)

DB_NAME = 'applicant.db'

# only a row with a final status stops another being logged; see storage.plan_rows
FINAL = '(status IS NULL OR status NOT IN ({}))'.format(
    ', '.join("'{}'".format(status) for status in sorted(PROVISIONAL))
)

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
    # 2 (0.2.0): company financials, which FinancialsTracker kept in its JSON alone
    (
        """CREATE TABLE financial_documents (
            document TEXT PRIMARY KEY,
            extra TEXT NOT NULL      -- any top-level keys beside 'companies', kept as found
        )""",
        """CREATE TABLE financials (
            document TEXT NOT NULL,
            key TEXT NOT NULL,       -- '<source>:<company id>', see financials.tracker
            seq INTEGER NOT NULL,    -- export order
            source TEXT,
            company TEXT,
            last_checked TEXT,
            entry TEXT NOT NULL,     -- the entry as the file holds it, snapshots aside
            PRIMARY KEY (document, key)
        )""",
        """CREATE TABLE financial_snapshots (
            document TEXT NOT NULL,
            key TEXT NOT NULL,
            seq INTEGER NOT NULL,
            recorded_at TEXT,
            snapshot TEXT NOT NULL,
            PRIMARY KEY (document, key, seq)
        )""",
        'CREATE INDEX financial_history ON financial_snapshots (key, recorded_at)',
    ),
    RANKING_MIGRATION,
    FEEDBACK_MIGRATION,
    PERSONALIZATION_MIGRATION,
    BENCHMARK_MIGRATION,
    COMPANY_EVIDENCE_MIGRATION,
    RESEARCH_MIGRATION,
    (
        """CREATE TABLE financial_pending_exports (
            document TEXT PRIMARY KEY,
            baseline BLOB,
            baseline_digest TEXT,
            intended BLOB NOT NULL,
            intended_digest TEXT NOT NULL
        )""",
    ),
    (
        """CREATE TABLE pending_exports (
            kind TEXT NOT NULL,
            document TEXT NOT NULL,
            baseline BLOB,
            baseline_digest TEXT,
            intended BLOB NOT NULL,
            intended_digest TEXT NOT NULL,
            PRIMARY KEY (kind, document)
        )""",
        """CREATE TABLE export_resolutions (
            token TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            document TEXT NOT NULL,
            choice TEXT NOT NULL,
            archived_path TEXT NOT NULL,
            recorded_at TEXT NOT NULL
        )""",
    ),
    (
        """CREATE TABLE application_attempt_journals (
            log TEXT PRIMARY KEY,
            payload TEXT NOT NULL
        )""",
    ),
    (
        """CREATE TABLE application_projection_receipts (
            log TEXT NOT NULL,
            event TEXT NOT NULL,
            PRIMARY KEY (log, event)
        )""",
    ),
    (
        """CREATE TABLE application_csv_snapshots (
            log TEXT PRIMARY KEY,
            content BLOB NOT NULL
        )""",
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
            make_private(self.path)
            self.db = sqlite3.connect(self.path)
            try:
                configure_connection(self.db)
                if single_attempt():
                    self.db.execute('PRAGMA busy_timeout = 0')
                # REPLACE deletes conflicting rows. Fire delete guards for that
                # path too, so it cannot bypass append-only history triggers.
                self.db.execute('PRAGMA recursive_triggers = ON')
                if self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                    raise sqlite3.DatabaseError('foreign key enforcement is unavailable')
                # WAL is a persistent database setting, configured outside a
                # transaction. Refuse an unsupported mode rather than claiming
                # concurrent readers are supported when they are not.
                mode = self.db.execute('PRAGMA journal_mode').fetchone()[0]
                if mode.lower() != 'wal':
                    mode = self.db.execute('PRAGMA journal_mode = WAL').fetchone()[0]
                if mode.lower() != 'wal':
                    raise sqlite3.DatabaseError(f'WAL journal mode unavailable: {mode}')
                self._migrate()
            except BaseException:
                self.db.close()
                raise
        except (OSError, sqlite3.Error) as error:
            if sqlite_busy(error):
                raise DatabaseBusy(
                    f'database busy: {self.path}; retry after the writer finishes'
                ) from error
            raise StoreError(f'could not open {self.path}: {error}') from error

    @classmethod
    def open_readonly(cls, path: str | os.PathLike[str]) -> Store:
        """Open existing current-schema data without initializing or migrating it.

        SQLite may still use WAL sidecars. Never mark a live database immutable.
        Missing files raise StoreError rather than creating an empty database.
        """
        store = cls.__new__(cls)
        store.path = Path(path)
        store.root = store.path.resolve().parent
        try:
            store.db = sqlite3.connect(store.path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                configure_connection(store.db, writable=False)
                if single_attempt():
                    store.db.execute('PRAGMA busy_timeout = 0')
                store.db.execute('PRAGMA query_only = ON')
                version = store.db.execute('PRAGMA user_version').fetchone()[0]
                if version < len(MIGRATIONS):
                    raise StoreError(
                        f'database schema {version} requires a writable upgrade to {len(MIGRATIONS)}; run applicant db upgrade --database PATH'
                    )
                if version > len(MIGRATIONS):
                    raise StoreError(
                        f'database schema {version} is newer than supported {len(MIGRATIONS)}'
                    )
            except BaseException:
                store.db.close()
                raise
        except (OSError, sqlite3.Error) as error:
            if sqlite_busy(error):
                raise DatabaseBusy(
                    f'database busy: {store.path}; retry after the writer finishes'
                ) from error
            raise StoreError(f'could not read {store.path}: {error}') from error
        return store

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Keep related queries on one snapshot, without acquiring a writer lock."""
        if self.db.in_transaction:
            raise StoreError('read snapshot requires a connection without an active transaction')
        try:
            self.db.execute('BEGIN')
            yield
        except sqlite3.Error as error:
            raise StoreError(f'{self.path}: {error}') from error
        finally:
            self.db.rollback()

    def listing_items_snapshot(self, file: str | os.PathLike[str]) -> list[dict]:
        """Stored collection in input order; never sync, restore or export a file."""
        try:
            rows = self.db.execute(
                'SELECT payload FROM jobs WHERE listing = ? ORDER BY seq', (self.name(file),)
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        except (sqlite3.Error, ValueError) as error:
            raise StoreError(f'could not read listing {file}: {error}') from error

    @classmethod
    def beside(cls, file: str | os.PathLike[str]) -> Store:
        """The store for a file: applicant.db in the same directory."""
        return cls(Path(file).resolve().parent / DB_NAME)

    @classmethod
    @contextmanager
    def managed(cls, file, kind, *, wait=False):
        """Lock and check an existing collection before writable initialization."""
        target = Path(file).resolve()
        with financials_lock(target, wait=wait):
            database = target.parent / DB_NAME
            if database.exists():
                with (
                    closing(
                        sqlite3.connect(
                            database.as_uri() + '?mode=ro',
                            uri=True,
                            timeout=0 if single_attempt() else 5,
                        )
                    ) as db,
                    db,
                ):
                    db.execute('BEGIN')
                    cls._managed_alias(db, kind, target.name, target)
            with cls(database) as store:
                yield store, target

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
        while True:
            # Read the version only after acquiring the writer lock. Another
            # initializer may have upgraded the database while we waited.
            self.db.execute('BEGIN IMMEDIATE')
            try:
                # BEGIN creates WAL sidecars on first use. Restrict them before
                # migrations or application writes put any data in the WAL.
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(self.path) + suffix)
                    if sidecar.exists():
                        make_private(sidecar)
                version = self.db.execute('PRAGMA user_version').fetchone()[0]
                if version > len(MIGRATIONS):
                    raise sqlite3.DatabaseError(
                        f'database schema {version} is newer than supported {len(MIGRATIONS)}'
                    )
                if version == len(MIGRATIONS):
                    self.db.commit()
                    return
                number = version + 1
                for statement in MIGRATIONS[version]:
                    self.db.execute(statement)
                self.db.execute(f'PRAGMA user_version = {number}')
            except BaseException:
                self.db.rollback()
                raise
            self.db.commit()
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

    def _holds(self, name: str, table: str, column: str) -> bool:
        """Whether the database has anything for this collection."""
        found = self.db.execute(f'SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1', (name,))
        return found.fetchone() is not None

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

    def _managed_name(self, kind, file):
        return self._managed_alias(self.db, kind, self.name(file), file)

    @staticmethod
    def _managed_alias(db, kind, name, file):
        if os.name != 'nt':
            return name
        from ..windows_paths import directory_handle, insensitive_for

        target = Path(file).resolve()
        if target.parent.is_dir():
            with directory_handle(target.parent, create=False) as parent:
                if not insensitive_for(parent, target.parent, target.name):
                    return name
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources = {'jobs': ('jobs', 'listing'), 'applications': ('applications', 'log')}
        keys = []
        if kind in sources:
            table, column = sources[kind]
            if table in tables:
                keys.extend(row[0] for row in db.execute(f'SELECT DISTINCT {column} FROM {table}'))
        if 'pending_exports' in tables:
            keys.extend(
                row[0]
                for row in db.execute(
                    'SELECT document FROM pending_exports WHERE kind=?',
                    (kind,),
                )
            )
        if 'documents' in tables:
            keys.extend(row[0] for row in db.execute('SELECT name FROM documents'))
        matching = {key for key in keys if os.path.normcase(key) == os.path.normcase(name)}
        if len(matching) > 1:
            raise ExportConflict(
                f'{file}: conflicting collection keys {sorted(matching)!r}; '
                'back up the database and review the histories'
            )
        return next(iter(matching), name)

    def _sync_listing(self, file: Path, quarantine: bool) -> str | None:
        """Bring the collection in line with the file, if the file moved on.

        -> the collection name, or None when the file is unreadable and was
        left in place (a read that must not destroy it): the caller then sees
        what `load_jobs` would, an empty listing.
        """
        name = self._managed_name('jobs', file)
        self._reconcile_export('jobs', name, file)
        baseline = _financials_bytes(file)
        if not self._stale(name, file, 'jobs', 'listing'):
            return name

        existed = file.exists()
        document = read_document(file, quarantine=quarantine)
        if not quarantine and existed and not document and not _is_empty_object(file):
            return None  # unreadable, kept; read_document has said so
        if existed and not file.exists() and self._holds(name, 'jobs', 'listing'):
            # unreadable and moved aside: no one meant that as "these are my
            # jobs now", and the database still has them - write them back
            with self._transaction() as db:
                kept = [
                    json.loads(payload)
                    for (payload,) in db.execute(
                        'SELECT payload FROM jobs WHERE listing = ? ORDER BY seq', (name,)
                    )
                ]
                self._stage_export(db, 'jobs', name, None, document_bytes({'list': kept}))
            self._reconcile_export('jobs', name, file)
            logger.warning(f'{file}: rewritten from the {len(kept)} job(s) in {self.path}')
            return name

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
            self._remember_financials(db, name, _bytes_digest(baseline) if file.exists() else None)
        if existed:
            logger.info(f'{file}: imported {len(items)} job(s) into {self.path}')
        return name

    def job_items(self, file: str | os.PathLike[str]) -> list[dict]:
        """Every stored record of a listing, in the order it was first seen."""
        target = Path(file).resolve()
        with financials_lock(target):
            return self._job_items(target)

    def _job_items(self, target):
        name = self._sync_listing(target, quarantine=False)
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
        target = Path(file).resolve()
        with financials_lock(target):
            return self._save_jobs(jobs, target)

    def _save_jobs(self, jobs, target):
        name = self._sync_listing(target, quarantine=True)
        assert name is not None  # quarantine moved anything unreadable aside
        baseline = _financials_bytes(target)
        with self._transaction() as db:
            before = self._count(db, name)
            (last,) = db.execute(
                'SELECT COALESCE(MAX(seq), -1) FROM jobs WHERE listing = ?', (name,)
            ).fetchone()
            for job in jobs:
                item = job.to_dict()
                prior = db.execute(
                    'SELECT payload FROM jobs WHERE listing=? AND key=?', (name, _key(item))
                ).fetchone()
                if prior is not None:
                    metadata = json.loads(prior[0]).get('enrichment_v1')
                    if metadata is not None:
                        item['enrichment_v1'] = metadata
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
            self._stage_export(db, 'jobs', name, baseline, document_bytes({'list': items}))
        self._reconcile_export('jobs', name, target)
        logger.info(f'{target}: {total - before} new job(s), {total} stored')
        return total

    def update_listing_payloads(self, target, update):
        """Resolve pending posting revisions and stage the resulting listing."""
        from ...domain.dedupe import key

        name = self._sync_listing(target, quarantine=True)
        assert name is not None
        baseline = _financials_bytes(target)
        with self._transaction() as db:
            db.execute('BEGIN IMMEDIATE')
            payloads = db.execute('SELECT payload FROM jobs WHERE listing=? ORDER BY seq', (name,))
            records = (json.loads(payload) for (payload,) in payloads)
            items = {key(item): item for item in records}
            changed = update(items)
            if changed:
                intended = document_bytes({'list': list(items.values())})
                self._replace_managed(db, 'jobs', name, intended)
                self._stage_export(db, 'jobs', name, baseline, intended)
        if changed:
            self._reconcile_export('jobs', name, target)
        return changed

    def _count(self, db: sqlite3.Connection, name: str) -> int:
        return db.execute('SELECT COUNT(*) FROM jobs WHERE listing = ?', (name,)).fetchone()[0]

    # -- the application log ----------------------------------------------

    def _application_snapshot(self, name):
        row = self.db.execute(
            'SELECT content FROM application_csv_snapshots WHERE log=?', (name,)
        ).fetchone()
        return row[0] if row else None

    def _sync_log(self, file: Path) -> str:
        name = self._managed_name('applications', file)
        self._reconcile_export('applications', name, file)
        raw = _financials_bytes(file)
        snapshot = self._application_snapshot(name)
        if snapshot is not None:
            try:
                ApplicationCSV(snapshot)
            except ValueError as error:
                raise StoreError(f'{file}: invalid committed application CSV snapshot') from error
        if raw is not None:
            if raw != snapshot or _bytes_digest(raw) != self._known_digest(name):
                try:
                    ApplicationCSV(raw)
                except ValueError as error:
                    guidance = (
                        'preview restoration with store resolve-export --use sqlite'
                        if snapshot is not None
                        else 'back up and repair or restore the CSV'
                    )
                    raise ExportConflict(f'{file}: invalid application CSV; {guidance}') from error
                with self._transaction() as db:
                    self._replace_managed(db, 'applications', name, raw)
                    self._remember_financials(db, name, _bytes_digest(raw))
            return name

        if snapshot is None:
            existing = [
                json.loads(row[0])
                for row in self.db.execute(
                    'SELECT row FROM applications WHERE log=? ORDER BY seq', (name,)
                )
            ]
            if not existing:
                return name
            logger.warning(
                '%s: reconstructing legacy CSV; original layout and duplicate columns are unavailable',
                file,
            )
            snapshot = _csv_bytes(existing)
        with self._transaction() as db:
            self._replace_managed(db, 'applications', name, snapshot)
            self._stage_export(db, 'applications', name, None, snapshot)
        self._reconcile_export('applications', name, file)
        return name

    def application_rows(self, file: str | os.PathLike[str]) -> list[dict]:
        target = Path(file).resolve()
        with financials_lock(target):
            return self._application_rows(target)

    def _application_rows(self, target):
        name = self._sync_log(target)
        return [
            json.loads(row)
            for (row,) in self.db.execute(
                'SELECT row FROM applications WHERE log = ? ORDER BY seq', (name,)
            )
        ]

    def project_application(self, target, event_id, prepare):
        """Commit one projection under the caller's export lock."""
        name = self._sync_log(target)
        if self.db.execute(
            'SELECT 1 FROM application_projection_receipts WHERE log=? AND event=?',
            (name, event_id),
        ).fetchone():
            return
        baseline = self._application_snapshot(name)
        original = baseline
        intended = prepare(original)
        with self._transaction() as db:
            self._replace_managed(db, 'applications', name, intended)
            self._stage_export(db, 'applications', name, baseline, intended)
            db.execute(
                'INSERT INTO application_projection_receipts VALUES (?, ?)', (name, event_id)
            )
        self._reconcile_export('applications', name, target)

    def application_counts(self, file: str | os.PathLike[str]) -> dict[str, int]:
        """status -> rows holding it; an empty status counts as 'unknown'."""
        target = Path(file).resolve()
        with financials_lock(target):
            return self._application_counts(target)

    def _application_counts(self, target):
        name = self._sync_log(target)
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
        target = Path(file).resolve()
        with financials_lock(target, wait=True):
            return self._record_applications(entries, target)

    def _record_applications(self, entries, target):
        name = self._sync_log(target)
        baseline = self._application_snapshot(name)
        reader = self.db

        def seen(source: str | None, job_id: str | None) -> bool:
            # the CSV holds a missing id as '', and a job's missing id is None:
            # as in the file backend, the two never match
            return (
                reader.execute(
                    'SELECT 1 FROM applications WHERE log = ? AND source IS ? AND source_id IS ? '
                    f'AND {FINAL} LIMIT 1',
                    (name, source, job_id),
                ).fetchone()
                is not None
            )

        def boards_for(mark: str) -> set[str | None]:
            return {
                source
                for (source,) in reader.execute(
                    'SELECT DISTINCT source FROM applications WHERE log = ? AND fingerprint = ? '
                    f'AND {FINAL}',
                    (name, mark),
                )
            }

        fresh = plan_rows(entries, seen=seen, boards_for=boards_for)
        if not fresh:
            logger.debug(f'{target}: nothing new to record')
            return 0

        layout = ApplicationCSV(baseline)
        for row in fresh:
            layout.set(None, row)
        intended = layout.render()
        with self._transaction() as db:
            self._replace_managed(db, 'applications', name, intended)
            self._stage_export(db, 'applications', name, baseline, intended)
        self._reconcile_export('applications', name, target)
        logger.info(f'{target}: recorded {len(fresh)} application(s)')
        return len(fresh)

    # -- company financials -----------------------------------------------

    @classmethod
    def preflight_financials(cls, file: Path) -> None:
        """Check a legacy or current database before writable initialization."""
        database = file.parent / DB_NAME
        if os.name == 'nt' and database.exists():
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db, db:
                db.execute('BEGIN')
                cls._financials_alias(db, file.name, file)

    @staticmethod
    def _financials_alias(db: sqlite3.Connection, name: str, file: Path) -> str:
        if os.name != 'nt':
            return name
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        keys = {
            key
            for table in (
                'financial_pending_exports',
                'financial_documents',
                'financials',
                'financial_snapshots',
            )
            if table in tables
            for (key,) in db.execute(f'SELECT DISTINCT document FROM {table}')
            if os.path.normcase(key) == os.path.normcase(name)
        }
        if len(keys) > 1:
            raise ExportConflict(
                f'{file}: conflicting financials collection keys {sorted(keys)!r}. '
                'Back up the database and review both histories before retrying.'
            )
        return next(iter(keys), name)

    def _financials_name(self, file: Path) -> str:
        return self._financials_alias(self.db, self.name(file), file)

    def _sync_financials(self, file: Path) -> tuple[str, bytes | None]:
        """Recover staged writes before considering the file as an input."""
        name = self._financials_name(file)
        self._reconcile_financials(name, file)
        baseline = _financials_bytes(file)
        digest = _bytes_digest(baseline)
        if digest == self._known_digest(name):
            return name, baseline
        document = decode_document(file, baseline, quarantine=True) if baseline is not None else {}
        if (
            baseline is not None
            and not file.exists()
            and self._holds(name, 'financial_documents', 'document')
        ):
            self._save_financials(self._financials_from_db(name), file, None)
            logger.warning(f'{file}: rewritten from the history in {self.path}')
            return name, _financials_bytes(file)
        if not file.exists():
            baseline = digest = None
        with self._transaction() as db:
            self._replace_financials(db, name, document)
            self._remember_financials(db, name, digest)
        companies = document.get('companies')
        if isinstance(companies, dict) and companies:
            logger.info(f'{file}: imported {len(companies)} company record(s) into {self.path}')
        return name, baseline

    @staticmethod
    def _remember_financials(db: sqlite3.Connection, name: str, digest: str | None) -> None:
        db.execute(
            'INSERT INTO documents (name, digest) VALUES (?, ?) '
            'ON CONFLICT (name) DO UPDATE SET digest = excluded.digest',
            (name, digest),
        )

    def _stage_financials(
        self, name: str, document: dict, baseline: bytes | None
    ) -> tuple[bytes, str]:
        intended = document_bytes(document)
        intended_digest = hashlib.sha256(intended).hexdigest()
        with self._transaction() as db:
            self._replace_financials(db, name, document)
            db.execute(
                'INSERT OR REPLACE INTO financial_pending_exports VALUES (?, ?, ?, ?, ?)',
                (name, baseline, _bytes_digest(baseline), intended, intended_digest),
            )
        return intended, intended_digest

    def _reconcile_financials(self, name: str, file: Path) -> None:
        pending = self.db.execute(
            'SELECT baseline, baseline_digest, intended, intended_digest '
            'FROM financial_pending_exports WHERE document = ?',
            (name,),
        ).fetchone()

        def merge(baseline, current, intended):
            merged = merge_financial_exports(file, baseline, current, intended)
            return self._stage_financials(name, merged, current)

        def acknowledge(digest):
            with self._transaction() as db:
                self._remember_financials(db, name, digest)
                db.execute('DELETE FROM financial_pending_exports WHERE document = ?', (name,))

        reconcile(
            file, pending, acknowledge, merge=merge, publish=write_document_bytes, sleep=time.sleep
        )

    def _stage_export(self, db, kind, name, baseline, intended):
        db.execute(
            'INSERT OR REPLACE INTO pending_exports VALUES (?, ?, ?, ?, ?, ?)',
            (kind, name, baseline, _bytes_digest(baseline), intended, _bytes_digest(intended)),
        )

    def _reconcile_export(self, kind, name, target):
        pending = self.db.execute(
            'SELECT baseline, baseline_digest, intended, intended_digest FROM pending_exports '
            'WHERE kind=? AND document=?',
            (kind, name),
        ).fetchone()

        def acknowledge(digest):
            with self._transaction() as db:
                removed = db.execute(
                    'DELETE FROM pending_exports WHERE kind=? AND document=? AND intended_digest=? '
                    'AND baseline_digest IS ?',
                    (kind, name, digest, revision[0]),
                )
                if removed.rowcount != 1:
                    raise ExportConflict(
                        f'{target}: pending publication changed before acknowledgement'
                    )
                self._remember_financials(db, name, digest)

        if pending is None:
            return
        revision = [pending[1]]

        def merge(baseline, current, intended):
            from .managed_merge import merge_export

            output = merge_export(kind, target, baseline, current, intended)
            with self._transaction() as db:
                self._replace_managed(db, kind, name, output)
                self._stage_export(db, kind, name, current, output)
            revision[0] = _bytes_digest(current)
            return output, _bytes_digest(output)

        reconcile(
            target,
            pending,
            acknowledge,
            merge=merge if kind != 'ratings' else None,
            publish=write_document_bytes,
            sleep=time.sleep,
            protect_deletion=kind == 'applications',
        )

    def _replace_managed(self, db, kind, name, content):
        from .managed_merge import parse_applications, parse_listing

        if kind == 'jobs':
            items = parse_listing(content)['list']
            db.execute('DELETE FROM jobs WHERE listing=?', (name,))
            db.executemany(
                'INSERT INTO jobs (listing,key,seq,fingerprint,payload) VALUES (?,?,?,?,?)',
                [
                    (name, _key(item), i, fingerprint(item), json.dumps(item, ensure_ascii=False))
                    for i, item in enumerate(items)
                ],
            )
        elif kind == 'applications':
            rows = parse_applications(content)
            db.execute(
                'INSERT INTO application_csv_snapshots (log, content) VALUES (?,?) '
                'ON CONFLICT(log) DO UPDATE SET content=excluded.content',
                (name, content),
            )
            db.execute('DELETE FROM applications WHERE log=?', (name,))
            db.executemany(
                'INSERT INTO applications (log,seq,source,source_id,fingerprint,status,row) '
                'VALUES (?,?,?,?,?,?,?)',
                [_application(name, i, row) for i, row in enumerate(rows)],
            )

    def financials_document(self, file: str | os.PathLike[str]) -> dict:
        """The history document, rebuilt exactly as the file holds it."""
        target = Path(file).resolve()
        with financials_lock(target):
            return self._load_financials(target)[0]

    def _load_financials(self, target: Path) -> tuple[dict, bytes | None]:
        name, baseline = self._sync_financials(target)
        return self._financials_from_db(name), baseline

    def _financials_from_db(self, name: str) -> dict:
        extra = self.db.execute(
            'SELECT extra FROM financial_documents WHERE document = ?', (name,)
        ).fetchone()
        document: dict = json.loads(extra[0]) if extra else {}
        snapshots: dict[str, list] = {}
        for company, snapshot in self.db.execute(
            'SELECT key, snapshot FROM financial_snapshots WHERE document = ? ORDER BY key, seq',
            (name,),
        ):
            snapshots.setdefault(company, []).append(json.loads(snapshot))
        companies = {}
        for company, entry in self.db.execute(
            'SELECT key, entry FROM financials WHERE document = ? ORDER BY seq', (name,)
        ):
            record = json.loads(entry)
            if 'snapshots' in record:  # a placeholder, keeping the key where it was
                record['snapshots'] = snapshots.get(company, [])
            companies[company] = record
        if 'companies' in document or companies:
            document['companies'] = companies
        return document

    def save_financials(self, document: dict, file: str | os.PathLike[str]) -> None:
        """Replace the stored history with `document` and export it.

        The export is `document` itself, written the way the file backend
        writes it, so the two backends' files are the same bytes.
        """
        target = Path(file).resolve()
        with financials_lock(target):
            _, baseline = self._sync_financials(target)
            self._save_financials(document, target, baseline)

    def _save_financials(self, document: dict, target: Path, baseline: bytes | None) -> None:
        name = self._financials_name(target)
        self._stage_financials(name, document, baseline)
        self._reconcile_financials(name, target)

    def financial_history(self, file: str | os.PathLike[str], company: str) -> list[dict]:
        """Every snapshot of one company (`<source>:<id>`), oldest first."""
        target = Path(file).resolve()
        with financials_lock(target):
            name, _ = self._sync_financials(target)
            return [
                json.loads(snapshot)
                for (snapshot,) in self.db.execute(
                    'SELECT snapshot FROM financial_snapshots WHERE document = ? AND key = ? '
                    'ORDER BY seq',
                    (name, company),
                )
            ]

    def _replace_financials(self, db: sqlite3.Connection, name: str, document: dict) -> None:
        db.execute('DELETE FROM financial_documents WHERE document = ?', (name,))
        db.execute('DELETE FROM financials WHERE document = ?', (name,))
        db.execute('DELETE FROM financial_snapshots WHERE document = ?', (name,))
        # everything but the companies, with a None marker where 'companies' sat,
        # so the rebuilt document has its keys in the order the file had them
        extra = {key: (None if key == 'companies' else value) for key, value in document.items()}
        db.execute(
            'INSERT INTO financial_documents (document, extra) VALUES (?, ?)',
            (name, json.dumps(extra, ensure_ascii=False)),
        )
        companies = document.get('companies')
        if not isinstance(companies, dict):
            return
        for seq, (company, record) in enumerate(companies.items()):
            if not isinstance(record, dict):
                continue
            snapshots = record.get('snapshots')
            entry = {k: (None if k == 'snapshots' else v) for k, v in record.items()}
            db.execute(
                'INSERT INTO financials (document, key, seq, source, company, last_checked, entry) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (
                    name,
                    company,
                    seq,
                    record.get('source'),
                    record.get('company'),
                    record.get('last_checked'),
                    json.dumps(entry, ensure_ascii=False),
                ),
            )
            if isinstance(snapshots, list):
                db.executemany(
                    'INSERT INTO financial_snapshots (document, key, seq, recorded_at, snapshot) '
                    'VALUES (?, ?, ?, ?, ?)',
                    [
                        (
                            name,
                            company,
                            index,
                            snapshot.get('recorded_at') if isinstance(snapshot, dict) else None,
                            json.dumps(snapshot, ensure_ascii=False),
                        )
                        for index, snapshot in enumerate(snapshots)
                    ],
                )

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

    def _publish_ratings(self, target, company, ratings, observations):
        name = self._managed_name('ratings', target)
        self._reconcile_export('ratings', name, target)
        baseline = _financials_bytes(target)
        intended = document_bytes(ratings if len(ratings) > 1 else ratings[0])
        with self._transaction() as db:
            db.executemany(
                'INSERT INTO ratings (company,source,fetched_at,payload) VALUES (?,?,?,?)',
                [
                    (
                        company.lower(),
                        rating['source'],
                        stamp,
                        json.dumps(rating, ensure_ascii=False),
                    )
                    for stamp, rating in observations
                ],
            )
            self._stage_export(db, 'ratings', name, baseline, intended)
        self._reconcile_export('ratings', name, target)


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


def _csv_bytes(rows):
    layout = ApplicationCSV(None)
    layout.header = list(
        dict.fromkeys([*layout.header, *(key for row in rows for key in row if key)])
    )
    for row in rows:
        layout.set(None, row)
    return layout.render()


def _export_log(target: Path, rows: list[dict]) -> None:
    write_document_bytes(target, _csv_bytes(rows))


__all__ = ['APPLIED_COLUMNS', 'DB_NAME', 'MIGRATIONS', 'Store']
