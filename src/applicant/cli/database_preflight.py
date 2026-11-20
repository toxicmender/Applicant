"""Resolve and inspect command database dependencies before external work."""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import TypeVar

from ..errors import DatabaseBusy, StoreError, sqlite_busy
from ..infra.store.database_paths import (
    EXPECTED_FILES,
    PINNED,
    DatabasePin,
    database_alias,
    file_identity_at,
    sidecars,
    sqlite_path,
)
from ..infra.store.database_schema import expected_schema, incompatible_object, objects
from ..infra.store.sqlite import DB_NAME, MIGRATIONS, Store
from ..interaction import Terminal
from ..settings import Settings
from .database import database_path
from .dispatch import Request

R = TypeVar('R', bound=Request)


def inspect(path: Path, *, integrity: bool = False, verify_step: bool = False) -> int | None:
    path = sqlite_path(path)
    try:
        stat = path.stat()
        if stat.st_size == 0:
            return 0
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only = ON')
            db.execute('BEGIN')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            actual = objects(db)
            supported = len(MIGRATIONS)
            expected = expected_schema(MIGRATIONS[: version if 0 < version <= supported else 1])
            incompatible = incompatible_object(actual, expected)
            if integrity or version < supported or incompatible:
                check = db.execute('PRAGMA quick_check').fetchone()[0]
                if check != 'ok':
                    raise StoreError(f'{path}: database integrity check failed: {check}')
            if version == 0 and not actual:
                return 0
            if version <= 0 or incompatible:
                raise StoreError(
                    f'{path}: unrecognized schema {version}; incompatible object {incompatible}'
                )
            if version > supported:
                raise StoreError(
                    f'{path}: database schema {version} requires a newer Applicant '
                    f'(supports {supported})'
                )
            if verify_step and version < supported:
                new_objects = expected_schema(MIGRATIONS[: version + 1]).keys() - expected.keys()
                if new_objects & actual.keys():
                    raise StoreError(
                        f'{path}: schema {version} contains objects from an uncommitted step'
                    )
            return version
    except FileNotFoundError:
        return None
    except (OSError, sqlite3.Error) as error:
        if sqlite_busy(error):
            raise DatabaseBusy(f'database busy: {path}') from error
        raise StoreError(f'{path}: database inspection failed: {error}') from error


def collection(settings, path, fallback):
    return Path(settings.path(path or fallback)).resolve().parent / DB_NAME


def targets(command, action, request, settings):
    if settings.store != 'sqlite' or command == 'db':
        return (), False
    files = settings.files
    if command == 'search':
        if request.list_saved:
            return (), False
        return (collection(settings, request.output, files.listing),), True
    if command == 'jobs':
        return (collection(settings, request.jobs, files.listing),), True
    if command in {'apply', 'status'}:
        listing = collection(settings, request.input, files.listing)
        log = collection(settings, request.log, files.applications)
        if command == 'status':
            return (listing, log), False
        journal_only = any(
            getattr(request, name, False)
            for name in (
                'reconcile',
                'reopen_attempt',
                'retry_attempt',
                'rotate_attestation_key',
                'import_attempts_from',
                'list_conflicts',
                'resolve_conflict',
            )
        )
        return ((log,) if journal_only else (listing, log)), not request.dry_run
    if command in {'reviews', 'financials'}:
        output = request.output or getattr(files, command)
        paths = (
            ()
            if os.path.normcase(output) == os.path.normcase(os.devnull)
            else (collection(settings, output, output),)
        )
        source = getattr(request, 'from_jobs', None)
        if source:
            paths = (*paths, collection(settings, source, files.listing))
        return paths, True
    if command == 'report' or (command == 'research' and action == 'template'):
        return (), False
    if (command, action) in {
        ('rank', 'replay'),
        ('survey', 'validate'),
        ('sources', 'list'),
        ('sources', 'show'),
    }:
        return (), False
    if not hasattr(request, 'database'):
        return (), False
    writable = action in {
        'create',
        'edit',
        'import',
        'record',
        'accept',
        'collect',
        'refresh',
        'technologies',
        'backfill-companies',
        'bridge-company',
        'capture-profile',
        'capture',
        'associate',
        'probe',
        'resolve-export',
        'fetch',
        'discover',
    }
    return (database_path(settings, request.database),), writable and not getattr(
        request, 'dry_run', False
    )


def identity(path: Path) -> tuple[int, int] | None:
    try:
        return file_identity_at(path)
    except OSError as error:
        raise StoreError(f'{path}: cannot identify database: {error}') from error


def manual(path: Path) -> str:
    return f'uv run applicant db upgrade --database "{path}"'


def verify(path: Path, previous: tuple[int, int] | None) -> None:
    if identity(path) != previous:
        raise StoreError(f'target_changed: {path}')


def migration_state(path: Path, stamp: tuple[int, int] | None, previous: int) -> str:
    try:
        verify(path, stamp)
        version = inspect(path, verify_step=True)
        if version == len(MIGRATIONS):
            return f'target version committed and verified ({version})'
        if version is not None and version >= previous:
            return f'active step did not commit; last verified schema {version}'
    except (StoreError, KeyboardInterrupt):
        pass
    return 'commit state unverified'


def pin_paths(paths: tuple[Path, ...]) -> dict[Path, DatabasePin]:
    groups: dict[Path | tuple[int, int], list[Path]] = {}
    names = sorted({Path(os.path.abspath(p)) for p in paths} | {p.resolve() for p in paths})
    for path in names:
        stamp = identity(path)
        groups.setdefault(stamp or path.resolve(), []).append(path)
    pins = {}
    for candidates in groups.values():
        aliases = tuple(database_alias(name) for name in candidates)
        locations = {alias.location for alias in aliases}
        bearing = {name: found for name in sorted(locations) if (found := sidecars(name))}
        if len(bearing) > 1:
            raise StoreError(
                'SQLite recovery sidecars at multiple aliases: '
                + ', '.join(str(sidecar) for found in bearing.values() for sidecar in found)
            )
        chosen = next(iter(bearing)) if bearing else candidates[0].resolve()
        if chosen.resolve() != chosen:
            raise StoreError(
                'SQLite would bypass recovery sidecars at '
                + ', '.join(map(str, bearing.get(chosen, ())))
                + f' via {chosen.resolve()}'
            )
        pin = DatabasePin(chosen, aliases)
        for alias in aliases:
            pins[alias.path] = pin
            pins[alias.resolved] = pin
    return pins


def dispatch(
    command: str,
    action: str | None,
    request: R,
    settings: Settings,
    handler: Callable[[R, Settings], int],
) -> int:
    paths, writable = targets(command, action, request, settings)
    pins = pin_paths(paths)
    token = PINNED.set(pins)
    expected = EXPECTED_FILES.set({})
    try:
        output = None
        if command == 'financials' and settings.store == 'sqlite':
            value = getattr(request, 'output', None) or settings.files.financials
            if os.path.normcase(value) != os.path.normcase(os.devnull):
                output = Path(settings.path(value)).resolve()
        preflight(tuple(sorted({pin.selected for pin in pins.values()})), writable, output)
        return handler(request, settings)
    finally:
        EXPECTED_FILES.reset(expected)
        PINNED.reset(token)


def preflight(
    paths: tuple[Path, ...],
    writable: bool,
    financials_output: Path | None = None,
) -> None:
    older = {}
    observed = {}
    supported = len(MIGRATIONS)
    for path in paths:
        stamp = identity(path)
        version = inspect(path)
        observed[path] = (stamp, version)
        expected = EXPECTED_FILES.get()
        if expected is not None:
            expected[path] = stamp
        if version is not None and version < supported:
            older[path] = version
    if older:
        if (
            financials_output is not None
            and sqlite_path(financials_output.parent / DB_NAME) in older
        ):
            from ..infra.store.financials_lock import financials_lock

            with financials_lock(financials_output):
                Store.preflight_financials(financials_output)
        commands = '\n'.join(manual(path) for path in older)
        interactive = (
            writable
            and sys.stdin.isatty()
            and sys.stderr.isatty()
            and os.environ.get('CI', '').lower() not in {'true', '1', 'yes'}
        )
        if interactive:
            summary = '\n'.join(
                f'{path}  schema {version} → {supported}' for path, version in older.items()
            )
            try:
                accepted = Terminal().ask(summary + '\nUpgrade databases and continue? [y/N] ')
            except EOFError:
                accepted = ''
            interactive = accepted.strip().lower() in {'y', 'yes'}
        if not interactive:
            print(commands, file=sys.stderr)
            raise StoreError('database upgrade required')
        outcomes = dict.fromkeys(older, 'unattempted')
        try:
            for path in older:
                outcomes[path] = 'upgrade started'
                verify(path, observed[path][0])
                with Store(path, verify_current=True):
                    pass
                if inspect(path) != supported:
                    raise StoreError(f'{path}: unable to verify upgrade')
                outcomes[path] = 'upgraded and verified'
        except (StoreError, KeyboardInterrupt) as error:
            print('Database upgrades stopped before command dispatch', file=sys.stderr)
            for path in observed:
                outcome = outcomes.get(path, 'already current or not requiring migration')
                if outcome == 'upgrade started':
                    outcome = migration_state(path, observed[path][0], older[path])
                    outcome = f'{"interrupted" if isinstance(error, KeyboardInterrupt) else "failed"}; {outcome}'
                print(f'{path}  {outcome}', file=sys.stderr)
            raise
    for path, (stamp, observed_version) in observed.items():
        verify(path, stamp)
        expected = supported if path in older else observed_version
        if inspect(path) != expected:
            raise StoreError(f'target_changed: {path}')
