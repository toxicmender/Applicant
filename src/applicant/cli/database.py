"""SQLite and export paths shared by CLI commands."""

from pathlib import Path

from ..errors import ConfigError
from ..infra.store.sqlite import DB_NAME
from ..settings import Settings


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
