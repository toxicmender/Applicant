"""Defer terminal interruption while finishing a bounded critical operation."""

import signal
import sys
from contextlib import contextmanager
from threading import current_thread, main_thread


@contextmanager
def defer_interrupt():
    if (
        current_thread() is not main_thread()
        or signal.getsignal(signal.SIGINT) != signal.default_int_handler
    ):
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
