"""Physical names for coordination files and checkout paths."""

import os
from pathlib import Path


def comparison_path(path: str, *, windows: bool) -> str:
    if not windows:
        return path
    prefix = path[:8].upper()
    if prefix == '\\\\?\\UNC\\':
        return '\\\\' + path[8:]
    if path.startswith('\\\\?\\'):
        suffix = path[4:]
        if (
            len(suffix) >= 3
            and suffix[0].isascii()
            and suffix[0].isalpha()
            and suffix[1:3] == ':\\'
        ):
            return suffix
        raise ValueError('expected a filesystem drive or UNC path')
    if path.startswith('\\\\.\\'):
        raise ValueError('expected a filesystem path, not a device namespace')
    return path


def physical_path(path: str | os.PathLike[str]) -> Path:
    """Resolve the longest existing ancestor, rejecting dangling links."""
    windows = os.name == 'nt'
    raw = os.path.expanduser(os.fspath(path))
    comparison_path(raw.replace('/', '\\') if windows else raw, windows=windows)
    current = Path(os.path.abspath(raw))
    suffix: list[str] = []
    while True:
        try:
            resolved = current.resolve(strict=True)
        except FileNotFoundError:
            if os.path.lexists(current) or current == current.parent:
                raise
            suffix.append(current.name)
            current = current.parent
        else:
            return resolved.joinpath(*reversed(suffix))


def lock_identity(path: Path) -> str:
    if os.name != 'nt':
        return str(path)
    from .windows_paths import directory_handle, insensitive_for

    resolved = physical_path(path)
    normalized = comparison_path(str(resolved), windows=True)
    if resolved.parent.is_dir():
        with directory_handle(resolved.parent, create=False) as parent:
            if not insensitive_for(parent, resolved.parent, resolved.name):
                return normalized
    # Preserve the spelling of the physical parent. Only the uncreated leaf folds.
    return os.path.join(os.path.dirname(normalized), os.path.normcase(os.path.basename(normalized)))
