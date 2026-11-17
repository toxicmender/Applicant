"""Verify the source before publishing a conflict backup."""

import os
from contextlib import contextmanager
from pathlib import Path

from ...errors import ExportConflict
from ...files import write_document_bytes
from .publication import read_bytes, sync_parent


@contextmanager
def _open_source(target):
    if os.name != 'nt':
        with target.open('rb') as handle:
            yield handle
        return

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # Deny writes during the capture while permitting other readers and rename.
    handle = create(str(target), 0x80000000, 0x1 | 0x4, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        close(handle)
        raise
    with os.fdopen(fd, 'rb') as stream:
        yield stream


def _stamp(stream):
    fd = stream.fileno()
    stat = os.fstat(fd)
    identity = stat.st_dev, stat.st_ino
    if os.name == 'nt':
        from msvcrt import get_osfhandle

        from ..windows_paths import _file_id

        native = _file_id(get_osfhandle(fd))
        if native is not None:
            identity = native
    return identity, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def verified_backup(target, backup, expected, discarded):
    def changed():
        return ExportConflict(f'{target}: file changed during capture; preview again')

    def save(check):
        if backup.exists():
            if read_bytes(backup) != discarded:
                raise ExportConflict(f'{backup}: existing backup differs')
            return

        def finalize(temporary, destination):
            if read_bytes(Path(temporary)) != discarded:
                raise ExportConflict(f'{destination}: backup verification failed')
            check()
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if read_bytes(destination) != discarded:
                    raise ExportConflict(f'{destination}: existing backup differs') from None
            Path(temporary).unlink()
            sync_parent(destination)

        write_document_bytes(backup, discarded, replace=finalize)

    if expected is None:

        def check_missing():
            if read_bytes(target) is not None:
                raise changed()

        save(check_missing)
        check_missing()
        return

    try:
        with _open_source(target) as source:
            initial = _stamp(source)

            def check():
                with _open_source(target) as reopened:
                    before = _stamp(reopened)
                    raw = reopened.read()
                    after = _stamp(reopened)
                    if before != initial or after != initial or raw != expected:
                        raise changed()

            for _ in range(2):
                source.seek(0)
                raw = source.read()
                if len(raw) != initial[1] or raw != expected or _stamp(source) != initial:
                    raise changed()
            check()
            save(check)
            check()
    except FileNotFoundError as error:
        raise changed() from error
