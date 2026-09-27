"""Logging for the command line, set up once in one place.

Every module logs through ``logging.getLogger(__name__)``, so everything sits
under the ``applicant`` logger and this module decides where it goes. Printed
output is the command's result; logging is the diagnostic trail beside it, on
stderr (and optionally a file), so the two never interleave in a pipe.

What is logged, where, and how it is protected (OWASP ASVS 5.0 V16):

* each entry carries a UTC timestamp, level and module (16.2.1, 16.2.2)
* control characters in messages are escaped, so text scraped from a website
  cannot forge extra log lines (16.4.1, CWE-117)
* registered secrets - API keys and tokens - are masked wherever they appear
  (16.2.5); passwords and cookies are never passed to a logger at all
* a log file is created readable by its owner only (16.4.2)
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time

ROOT = 'applicant'
FORMAT = '%(asctime)s %(levelname)s %(name)s: %(message)s'
DATE_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
MASK = '***'

# C0 controls and DEL, minus tab; newlines are what forge a log line
CONTROL = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')

# console verbosity: -q, default, -v, -vv
LEVELS = {-1: logging.ERROR, 0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}

_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Mask this value in every log line from here on. Short values are ignored,
    since masking a two letter string would shred ordinary words."""
    if value and len(value) >= 8:
        _secrets.add(value)


def escape(text: str) -> str:
    """'a\\nb' -> 'a\\\\nb': one entry stays one line whatever it contains."""
    return CONTROL.sub(lambda match: '\\x{:02x}'.format(ord(match.group())), text)


def redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, MASK)
    return text


class SafeFormatter(logging.Formatter):
    """Escapes and redacts the message; UTC timestamps with an explicit Z."""

    converter = time.gmtime

    def __init__(self):
        super().__init__(FORMAT, DATE_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        record = logging.makeLogRecord(record.__dict__)
        record.msg = escape(redact(record.getMessage()))
        record.args = None
        text = super().format(record)
        # tracebacks keep their newlines, but a secret inside one is still masked
        return redact(text)


# libraries whose records go through the same handlers, so they are escaped and
# redacted too; httpx logs every request at INFO, which is only wanted at -vv
THIRD_PARTY = ('httpx', 'httpcore')


def setup(verbosity: int = 0, log_file: str | None = None) -> logging.Logger:
    """Configure the ``applicant`` logger for a CLI run. Safe to call repeatedly."""
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(LEVELS[max(-1, min(2, verbosity))])
    handlers.append(console)

    if log_file:
        # created 0600 before logging opens it: logs can hold company names,
        # urls and error detail that are nobody else's business
        descriptor = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(descriptor)
        to_file = logging.FileHandler(log_file, encoding='utf-8')
        to_file.setLevel(logging.DEBUG)
        handlers.append(to_file)

    for handler in handlers:
        handler.setFormatter(SafeFormatter())

    for name, level in (
        (ROOT, logging.DEBUG),
        *((name, logging.DEBUG if verbosity >= 2 else logging.WARNING) for name in THIRD_PARTY),
    ):
        logger = logging.getLogger(name)
        for old in list(logger.handlers):
            logger.removeHandler(old)
            old.close()
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(level)
        logger.propagate = False
    return logging.getLogger(ROOT)
