"""Pin CLI SQLite paths and identities for the lifetime of a command."""

import os
from contextvars import ContextVar
from pathlib import Path

from ...errors import StoreError

SIDECARS = ('-wal', '-shm', '-journal')
PINNED: ContextVar[dict[Path, tuple[Path, tuple[Path, ...]]] | None] = ContextVar(
    'pinned_sqlite_paths', default=None
)
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


def has_sidecars(path: Path) -> bool:
    for suffix in SIDECARS:
        try:
            Path(str(path) + suffix).lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise StoreError(f'{path}: cannot inspect SQLite sidecars: {error}') from error
        else:
            return True
    return False


def sqlite_path(path: str | os.PathLike[str]) -> Path:
    path = Path(path).resolve()
    pin = (PINNED.get() or {}).get(path)
    if pin is None:
        return path
    selected, other_names = pin
    for alias in other_names:
        if has_sidecars(alias):
            raise StoreError(f'{selected}: recovery sidecars appeared at alias {alias}')
    return selected
