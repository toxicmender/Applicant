"""Physical names for coordination files and checkout paths."""

import errno
import os
import stat
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
    current = _follow_links(Path(os.path.abspath(raw)), windows=windows)
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


def _follow_links(path: Path, *, windows: bool) -> Path:
    """Reject repeated link identities and bound the number followed."""
    seen = set()
    for _ in range(33):
        parent = Path(path.anchor)
        for index, name in enumerate(path.parts[1:], start=1):
            parent /= name
            try:
                info = parent.lstat()
            except FileNotFoundError:
                return path
            if (
                not stat.S_ISLNK(info.st_mode)
                and getattr(info, 'st_reparse_tag', None) != 0xA0000003
            ):
                continue
            identity = (info.st_dev, info.st_ino)
            if identity in seen or len(seen) == 32:
                raise OSError(errno.ELOOP, 'too many filesystem links', parent)
            seen.add(identity)
            target = parent.parent / os.readlink(parent)
            comparison_path(str(target), windows=windows)
            if not os.path.lexists(target):
                raise FileNotFoundError(errno.ENOENT, 'dangling filesystem link', parent)
            path = Path(os.path.abspath(target)).joinpath(*path.parts[index + 1 :])
            break
        else:
            return path
    raise OSError(errno.ELOOP, 'too many filesystem links', path)


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
