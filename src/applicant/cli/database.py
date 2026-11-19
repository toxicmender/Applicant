"""SQLite and export paths shared by CLI commands."""

import sys
from collections.abc import Callable
from pathlib import Path

from ..errors import ConfigError
from ..infra.store.sqlite import DB_NAME, MIGRATIONS, Store
from ..settings import Settings
from .dispatch import AddParser, Request, actions, bind


def database_path(settings: Settings, database: str | None) -> Path:
    if settings.store != 'sqlite':
        raise ConfigError('this command requires --store sqlite')
    if database:
        return Path(settings.path(database))
    return Path(settings.path(settings.files.listing)).resolve().parent / DB_NAME


def export_path(settings: Settings, database: Path, export: str | None) -> Path | None:
    if export is None:
        return None
    target = Path(settings.path(export)).resolve()
    if target in {
        database.resolve(),
        *(Path(str(database) + s).resolve() for s in ('-wal', '-shm')),
    }:
        raise ConfigError('export must not replace database or sidecars')
    return target


class Upgrade(Request):
    database: str


def add_parser(add: AddParser, handler: Callable[[Upgrade, Settings], int]) -> None:
    action = actions(add('db', help='manage an existing Applicant database'))
    command = action('upgrade', help='upgrade an existing SQLite database')
    command.add_argument('--database', required=True)
    bind(command, Upgrade, handler)


def run_upgrade(args: Upgrade, settings: Settings) -> int:
    from ..infra.store.database_paths import EXPECTED_FILES, PINNED, file_identity_at, sqlite_path
    from .database_preflight import inspect, pin_paths

    try:
        original = database_path(settings, args.database)
        aliases = PINNED.set(pin_paths((original,)))
        database = sqlite_path(original)
        expectation = EXPECTED_FILES.set({database: file_identity_at(database)})
        try:
            version = inspect(database, integrity=True)
            if version is None:
                print(f'database not found: {database}', file=sys.stderr)
                return 4
            if version == len(MIGRATIONS):
                print(f'database already current at schema {version}: {database}')
                return 0
            with Store(database):
                pass
            inspect(database, integrity=True)
            print(f'database upgraded to schema {len(MIGRATIONS)}: {database}')
            return 0
        finally:
            EXPECTED_FILES.reset(expectation)
            PINNED.reset(aliases)
    except ConfigError as error:
        print(f'applicant db: {error}', file=sys.stderr)
        return 2
