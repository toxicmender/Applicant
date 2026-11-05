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
    try:
        database = database_path(settings, args.database)
        if not database.is_file():
            print(f'database not found: {database}', file=sys.stderr)
            return 4
        with Store(database):
            pass
        print(f'database upgraded to schema {len(MIGRATIONS)}: {database.resolve()}')
        return 0
    except ConfigError as error:
        print(f'applicant db: {error}', file=sys.stderr)
        return 2
