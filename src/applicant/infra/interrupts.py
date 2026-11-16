"""Defer terminal interruption while finishing a bounded critical operation."""

import signal
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from threading import current_thread, main_thread

_standalone = ContextVar('standalone_console', default=False)


@contextmanager
def standalone_console():
    token = _standalone.set(True)
    try:
        yield
    finally:
        _standalone.reset(token)


@contextmanager
def _windows_console_deferral():
    import ctypes
    from ctypes import wintypes

    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    pending = ctypes.c_long(0)

    @callback
    def remember(event):
        if event in (0, 1):  # CTRL_C_EVENT and CTRL_BREAK_EVENT
            pending.value = 1
            return True
        return False

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    install = kernel.SetConsoleCtrlHandler
    install.argtypes = [callback, wintypes.BOOL]
    install.restype = wintypes.BOOL
    if not install(remember, True):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        install(remember, False)
        if pending.value and sys.exc_info()[0] is None:
            raise KeyboardInterrupt


@contextmanager
def defer_interrupt():
    if (
        current_thread() is not main_thread()
        or signal.getsignal(signal.SIGINT) != signal.default_int_handler
    ):
        yield
        return
    if sys.platform == 'win32' and _standalone.get():
        with _windows_console_deferral():
            yield
        return
    interrupted = False

    def remember(_signum, _frame):
        nonlocal interrupted
        interrupted = True

    previous = signal.signal(signal.SIGINT, remember)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if interrupted and sys.exc_info()[0] is None:
            raise KeyboardInterrupt
