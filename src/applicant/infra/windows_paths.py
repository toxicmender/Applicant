"""Handle-relative opens for Windows coordination files."""

import ctypes
import os
from contextlib import contextmanager, suppress
from ctypes import wintypes
from functools import lru_cache
from uuid import uuid4


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ('length', ctypes.c_ushort),
        ('capacity', ctypes.c_ushort),
        ('text', wintypes.LPWSTR),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ('length', wintypes.ULONG),
        ('root', wintypes.HANDLE),
        ('name', ctypes.POINTER(_UnicodeString)),
        ('attributes', wintypes.ULONG),
        ('security', ctypes.c_void_p),
        ('quality', ctypes.c_void_p),
    ]


class _StatusBlock(ctypes.Structure):
    _fields_ = [('status', ctypes.c_void_p), ('information', ctypes.c_size_t)]


@lru_cache(maxsize=1)
def _apis() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    if os.name != 'nt':
        raise RuntimeError('Windows path APIs are unavailable')
    loader = ctypes.WinDLL
    kernel, native = (loader(name, use_last_error=True) for name in ('kernel32', 'ntdll'))
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    native.NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_StatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    ]
    native.NtCreateFile.restype = ctypes.c_long
    native.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
    native.RtlNtStatusToDosError.restype = wintypes.ULONG
    return kernel, native


def _win_error(code=None) -> OSError:
    if os.name != 'nt':
        raise RuntimeError('Windows errors are unavailable')
    return ctypes.WinError(code)


def case_sensitive(handle):
    kernel, _ = _apis()
    flags = wintypes.DWORD()
    if kernel.GetFileInformationByHandleEx(handle, 23, ctypes.byref(flags), ctypes.sizeof(flags)):
        return bool(flags.value & 1)
    return None


def _reparse_tag(handle):
    kernel, _ = _apis()
    attrs = (wintypes.DWORD * 2)()
    if not kernel.GetFileInformationByHandleEx(
        handle, 9, ctypes.byref(attrs), ctypes.sizeof(attrs)
    ):
        raise _win_error()
    return attrs[1] if attrs[0] & 0x400 else 0


def _check_directory(handle, path):
    tag = _reparse_tag(handle)
    if tag and (tag & 0xFFFF0FFF) != 0x9000001A and tag != 0x9000001C:
        raise OSError(f'cannot traverse reparse directory without resolving it: {path}')


def _open(parent, name, *, directory, create, insensitive, probe=False, probe_reader=False):
    _, native = _apis()
    encoded = len(name.encode('utf-16-le'))
    wide = ctypes.create_unicode_buffer(name)
    text = _UnicodeString(encoded, encoded + 2, ctypes.cast(wide, wintypes.LPWSTR))
    attrs = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent,
        ctypes.pointer(text),
        0x40 if insensitive else 0,
        None,
        None,
    )
    handle = wintypes.HANDLE()
    status = _StatusBlock()
    access = 0x001000A0 if directory else 0xC0100000
    # The no-reparse metadata open is supported for native relative paths.
    # Win32 applies OPEN_NO_RECALL on the root open.
    options = 0x00200020 | (1 if directory else 0x40)
    if probe:
        access |= 0x10000  # DELETE for delete-on-close
        options |= 0x1000
    disposition = 2 if probe else (3 if create else 1)
    share = 7 if probe or probe_reader else 3
    code = native.NtCreateFile(
        ctypes.byref(handle),
        access,
        ctypes.byref(attrs),
        ctypes.byref(status),
        None,
        0,
        share,
        disposition,
        options,
        None,
        0,
    )
    if code < 0:
        raise _win_error(native.RtlNtStatusToDosError(code))
    assert handle.value is not None
    return handle.value


def _alternate(path, name):
    original = path / name
    sibling = path / name.swapcase()
    try:
        return not os.path.samefile(original, sibling)
    except FileNotFoundError:
        return True if original.exists() else None
    except OSError:
        return None


def _file_id(handle):
    kernel, _ = _apis()
    identity = (ctypes.c_ubyte * 24)()
    if kernel.GetFileInformationByHandleEx(
        handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)
    ):
        return bytes(identity)
    return None


def directory_case(handle, path):
    known = case_sensitive(handle)
    if known is not None:
        return known
    with suppress(OSError), os.scandir(path) as entries:
        for entry in entries:
            name = entry.name
            if name.isascii() and name != name.swapcase() and not entry.is_symlink():
                answer = _alternate(path, name)
                if answer is not None:
                    return answer
    name = f'.applicant-case-{uuid4().hex}'
    kernel, _ = _apis()
    try:
        probe = _open(handle, name, directory=False, create=True, insensitive=False, probe=True)
    except OSError:
        return None
    try:
        try:
            sibling = _open(
                handle,
                name.swapcase(),
                directory=False,
                create=False,
                insensitive=False,
                probe_reader=True,
            )
        except FileNotFoundError:
            return True
        except OSError:
            return None
        try:
            original, alternative = _file_id(probe), _file_id(sibling)
            return (
                original != alternative
                if original is not None and alternative is not None
                else None
            )
        finally:
            kernel.CloseHandle(sibling)
    finally:
        kernel.CloseHandle(probe)


def insensitive_for(handle, path, name):
    supported = directory_case(handle, path)
    if supported is not None:
        return not supported
    with os.scandir(path) as entries:
        aliases = [entry.name for entry in entries if entry.name.casefold() == name.casefold()]
    if len(aliases) > 1:
        raise OSError(f'cannot establish a unique case identity under {path}: {name}')
    return True


@contextmanager
def directory_handle(path, *, create):
    kernel, _ = _apis()
    current = kernel.CreateFileW(path.anchor, 0xA0, 3, None, 3, 0x02300000, None)
    if current == ctypes.c_void_p(-1).value:
        raise _win_error()
    try:
        _check_directory(current, path.anchor)
        parent_path = type(path)(path.anchor)
        for name in path.parts[1:]:
            child = _open(
                current,
                name,
                directory=True,
                create=create,
                insensitive=insensitive_for(current, parent_path, name),
            )
            kernel.CloseHandle(current)
            current = child
            _check_directory(current, name)
            parent_path /= name
        yield current
    finally:
        kernel.CloseHandle(current)


def lock_descriptor(parent, path):
    if os.name != 'nt':
        raise RuntimeError('Windows lock descriptors are unavailable')
    from msvcrt import open_osfhandle

    kernel, _ = _apis()
    name = path.name
    handle = _open(
        parent,
        name,
        directory=False,
        create=True,
        insensitive=insensitive_for(parent, path.parent, name),
    )
    try:
        if _reparse_tag(handle):
            raise OSError(f'lock must not be a reparse point: {name}')
        return open_osfhandle(handle, os.O_RDWR | os.O_BINARY | os.O_NOINHERIT)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
