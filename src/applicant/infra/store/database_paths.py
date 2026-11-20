"""Pin CLI SQLite paths and identities for the lifetime of a command."""

import os
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from ...errors import StoreError

SIDECARS = ('-wal', '-shm', '-journal')


@dataclass(frozen=True)
class Alias:
    path: Path
    location: Path
    resolved: Path
    ancestor: Path
    ancestor_id: tuple[int, int]


@dataclass(frozen=True)
class DatabasePin:
    selected: Path
    aliases: tuple[Alias, ...]


PINNED: ContextVar[dict[Path, DatabasePin] | None] = ContextVar('pinned_sqlite_paths', default=None)
EXPECTED_FILES: ContextVar[dict[Path, tuple[int, int] | None] | None] = ContextVar(
    'expected_sqlite_files', default=None
)


def descriptor_identity(fd: int) -> tuple[int, int]:
    if os.name == 'nt':
        from ..windows_paths import file_identity

        return file_identity(fd)
    info = os.fstat(fd)
    return info.st_dev, info.st_ino


def file_identity_at(path: Path) -> tuple[int, int] | None:
    if os.name == 'nt':
        try:
            fd = os.open(path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            return descriptor_identity(fd)
        finally:
            os.close(fd)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def verify_identity(path: Path) -> None:
    expected = EXPECTED_FILES.get()
    if expected is not None and path in expected and file_identity_at(path) != expected[path]:
        raise StoreError(f'target_changed: {path}')


def register_created(path: Path, stamp: tuple[int, int]) -> None:
    expected = EXPECTED_FILES.get()
    if expected is not None and path in expected:
        expected[path] = stamp


def sidecars(path: Path) -> tuple[Path, ...]:
    found = []
    for suffix in SIDECARS:
        candidate = Path(str(path) + suffix)
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StoreError(f'{path}: cannot inspect SQLite sidecars: {error}') from error
        else:
            found.append(candidate)
    return tuple(found)


def sidecar_location(path: Path) -> Path:
    # Resolve directory aliases without following a symlink at the database name.
    return path.parent.resolve() / path.name


def directory_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def database_alias(path: Path) -> Alias:
    location = sidecar_location(path)
    for ancestor in path.parents:
        try:
            stamp = directory_identity(ancestor)
        except FileNotFoundError:
            continue
        return Alias(path, location, path.resolve(), ancestor, stamp)
    raise StoreError(f'{path}: cannot identify an existing parent directory')


def sqlite_path(path: str | os.PathLike[str]) -> Path:
    supplied = Path(os.path.abspath(path))
    path = supplied.resolve()
    pins = PINNED.get() or {}
    pin = pins.get(supplied) or pins.get(path)
    if pin is None:
        return path
    selected = pin.selected
    stamp = file_identity_at(selected)
    for alias in pin.aliases:
        try:
            valid_parent = directory_identity(alias.ancestor) == alias.ancestor_id
        except FileNotFoundError:
            valid_parent = False
        if (
            not valid_parent
            or sidecar_location(alias.path) != alias.location
            or alias.path.resolve() != alias.resolved
            or file_identity_at(alias.path) != stamp
        ):
            raise StoreError(f'target_changed: database alias {alias.path}')
        if alias.location != selected and (found := sidecars(alias.location)):
            raise StoreError(
                f'{selected}: recovery sidecars appeared at alias ' + ', '.join(map(str, found))
            )
    return selected
