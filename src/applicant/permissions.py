"""Private file permissions: POSIX modes or a protected owner-only Windows DACL.

Windows chmod only controls the read-only attribute. Use native security APIs
before writing sensitive data, and preserve an existing DACL for atomic exports.
No shell commands or additional dependencies are required by the application.
"""

from __future__ import annotations

import ctypes
import os
import stat
from functools import lru_cache
from pathlib import Path


class _WindowsSecurity:
    def __init__(self):
        from ctypes import wintypes

        loader = getattr(ctypes, 'WinDLL', None)
        if loader is None:
            raise OSError('Windows security APIs are unavailable')
        self.api = loader('advapi32', use_last_error=True)
        self.kernel = loader('kernel32', use_last_error=True)
        pointer = ctypes.c_void_p
        signatures = {
            'GetFileSecurityW': (
                [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    pointer,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            'SetFileSecurityW': ([wintypes.LPCWSTR, wintypes.DWORD, pointer], wintypes.BOOL),
            'GetSecurityDescriptorOwner': (
                [pointer, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)],
                wintypes.BOOL,
            ),
            'GetSecurityDescriptorControl': (
                [pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)],
                wintypes.BOOL,
            ),
            'ConvertSidToStringSidW': ([pointer, ctypes.POINTER(pointer)], wintypes.BOOL),
            'ConvertStringSecurityDescriptorToSecurityDescriptorW': (
                [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), pointer],
                wintypes.BOOL,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.kernel.LocalFree.argtypes = [pointer]
        self.kernel.LocalFree.restype = pointer

    @staticmethod
    def check(result):
        if not result:
            raise getattr(ctypes, 'WinError', OSError)(
                getattr(ctypes, 'get_last_error', lambda: 0)()
            )

    def read(self, path: str | Path):
        from ctypes import wintypes

        size = wintypes.DWORD()
        self.api.GetFileSecurityW(str(path), 5, None, 0, ctypes.byref(size))
        if not size.value:
            self.check(False)
        descriptor = ctypes.create_string_buffer(size.value)
        self.check(self.api.GetFileSecurityW(str(path), 5, descriptor, size, ctypes.byref(size)))
        return descriptor

    def write(self, path: str | Path, descriptor):
        from ctypes import wintypes

        control, revision = wintypes.WORD(), wintypes.DWORD()
        self.check(
            self.api.GetSecurityDescriptorControl(
                descriptor, ctypes.byref(control), ctypes.byref(revision)
            )
        )
        # Preserve whether inheritance was disabled on the source descriptor.
        protection = 0x80000000 if control.value & 0x1000 else 0x20000000
        self.check(self.api.SetFileSecurityW(str(path), 4 | protection, descriptor))

    def protect(self, path: str | Path):
        from ctypes import wintypes

        original = self.read(path)
        owner, defaulted = ctypes.c_void_p(), wintypes.BOOL()
        self.check(
            self.api.GetSecurityDescriptorOwner(
                original, ctypes.byref(owner), ctypes.byref(defaulted)
            )
        )
        sid = ctypes.c_void_p()
        self.check(self.api.ConvertSidToStringSidW(owner, ctypes.byref(sid)))
        try:
            sddl = f'D:P(A;;FA;;;{ctypes.wstring_at(sid)})'
        finally:
            self.kernel.LocalFree(sid)
        descriptor = ctypes.c_void_p()
        self.check(
            self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None
            )
        )
        try:
            self.write(path, descriptor)
        finally:
            self.kernel.LocalFree(descriptor)


@lru_cache(maxsize=1)
def _windows() -> _WindowsSecurity:
    return _WindowsSecurity()


def make_private(path: str | Path) -> None:
    """Remove non-owner access; errors propagate before sensitive data is written."""
    if os.name == 'nt':
        _windows().protect(path)
    else:
        os.chmod(path, 0o600)


def copy_permissions(source: str | Path, target: str | Path) -> None:
    """Preserve the source's access policy on an atomic replacement file."""
    if os.name == 'nt':
        security = _windows()
        security.write(target, security.read(source))
    else:
        os.chmod(target, stat.S_IMODE(Path(source).stat().st_mode))
