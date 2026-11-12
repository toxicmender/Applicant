"""Structured browser events that are safe to send to the run logger."""

import ipaddress
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from queue import Full
from urllib.parse import urlparse

logger = logging.getLogger('applicant.browser')
QUEUE_SIZE = 128
# Leave room for the queue's pickle and framing inside a 512-byte POSIX pipe write.
MAX_EVENT_BYTES = 448
NETWORK_CODES = frozenset(
    [
        'ERR_ABORTED',
        'ERR_NAME_NOT_RESOLVED',
        'ERR_CONNECTION_RESET',
        'ERR_CONNECTION_REFUSED',
        'ERR_CONNECTION_CLOSED',
        'ERR_CONNECTION_TIMED_OUT',
        'ERR_TIMED_OUT',
        'ERR_INTERNET_DISCONNECTED',
        'ERR_CERT_AUTHORITY_INVALID',
        'ERR_CERT_COMMON_NAME_INVALID',
        'ERR_CERT_DATE_INVALID',
        'ERR_SSL_PROTOCOL_ERROR',
        'ERR_PROXY_CONNECTION_FAILED',
        'ERR_TOO_MANY_REDIRECTS',
        'ERR_BLOCKED_BY_CLIENT',
        'ERR_HTTP2_PROTOCOL_ERROR',
        'ERR_ADDRESS_UNREACHABLE',
    ]
)


def safe_origin(url: str) -> str:
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
        if parsed.scheme not in ('http', 'https') or not host or '%' in host:
            return 'unknown'
        if ':' in host:
            host = f'[{ipaddress.IPv6Address(host)}]'
        else:
            host = host.encode('idna').decode('ascii')
            if not re.fullmatch(r'[a-z0-9.-]{1,253}', host):
                return 'unknown'
        authority = host if port is None else f'{host}:{port}'
        return f'{parsed.scheme}://{authority}'
    except (ValueError, UnicodeError):
        return 'unknown'


def network_code(message: str) -> str | None:
    return next(
        (
            code
            for code in re.findall(r'\bnet::(ERR_[A-Z0-9_]+)\b', message)
            if code in NETWORK_CODES
        ),
        None,
    )


def failure_details(error: BaseException, operation: str) -> dict:
    cls = type(error)
    frames = []
    current = error.__traceback__
    while current is not None:
        frame = current.tb_frame
        module = frame.f_globals.get('__name__', '')
        if module.startswith('applicant.'):
            frames.append(
                {'module': module, 'function': frame.f_code.co_name, 'lineno': current.tb_lineno}
            )
        current = current.tb_next
    message = getattr(error, 'message', '') if cls.__module__.startswith('playwright.') else ''
    code = network_code(message) if isinstance(message, str) else None
    category = 'unclassified'
    if code:
        category = 'network'
    elif isinstance(error, TimeoutError) or cls.__name__ == 'TimeoutError':
        category = 'timeout'
    elif cls.__name__ == 'TargetClosedError':
        category = 'target_closed'
    elif isinstance(message, str) and "Executable doesn't exist" in message:
        category = 'executable_missing'
    return {
        'operation': operation,
        'exception_module': cls.__module__,
        'exception_class': cls.__name__,
        'category': category,
        'network_code': code,
        'frames': frames[-8:],
    }


class WorkerDiagnostics:
    def __init__(self, queue, provider: str):
        self.queue = queue
        self.provider = provider
        self.started = time.monotonic()
        self.emitted = self.dropped = self.discarded = 0
        self.failures: dict[int, tuple[BaseException, dict]] = {}
        self.cleanup_failures: list[dict] = []

    def emit(self, operation: str, outcome: str, severity: int, fields: dict):
        payload = {
            **fields,
            'provider': self.provider,
            'pid': os.getpid(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'operation': operation,
            'outcome': outcome,
            'elapsed_ms': round((time.monotonic() - self.started) * 1000),
            'severity': severity,
        }
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(',', ':'))
            if len(encoded) > MAX_EVENT_BYTES:
                self.discarded += 1
                return
            self.queue.put_nowait(encoded)
        except Full:
            self.dropped += 1
        except (OSError, TypeError, ValueError):
            self.discarded += 1
        else:
            self.emitted += 1


active_diagnostics: ContextVar[WorkerDiagnostics | None] = ContextVar(
    'browser_diagnostics', default=None
)


def event(operation: str, outcome: str, *, severity: int = logging.DEBUG, **fields):
    diagnostics = active_diagnostics.get()
    if diagnostics is not None:
        diagnostics.emit(operation, outcome, severity, fields)
    else:
        logger.log(severity, 'browser %s %s %s', operation, outcome, fields)


@contextmanager
def stage(operation: str, **fields):
    event(operation, 'started', **fields)
    try:
        yield
    except BaseException as error:
        diagnostics = active_diagnostics.get()
        if diagnostics is None or id(error) not in diagnostics.failures:
            details = failure_details(error, operation)
            if diagnostics is not None:
                if len(diagnostics.failures) == 8:
                    del diagnostics.failures[next(iter(diagnostics.failures))]
                diagnostics.failures[id(error)] = (error, details)
                if operation == 'browser_teardown':
                    diagnostics.cleanup_failures.append(details)
                    diagnostics.cleanup_failures = diagnostics.cleanup_failures[-4:]
            event(
                operation,
                'failed',
                **fields,
                exception_class=details['exception_class'],
                category=details['category'],
                network_code=details['network_code'],
            )
        raise
    else:
        event(operation, 'completed', **fields)
