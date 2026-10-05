"""Where the tool's running commentary goes, and what may reach it.

A scrape talks while it works - which board answered, how many postings
survived, which one refused. People do two things with that talk on a long
scrape: turn the noise down, or keep a record of what happened while they were
not watching.

So the library logs and the commands print. A message *about* the work in
progress goes to a logger here; the answer a subcommand was asked for -
`status`' tally, `rates`' table, where a file was written - stays a `print`,
because that is the command's output rather than its commentary, and silencing
it with `--quiet` would be silencing the answer. The commentary goes to stderr,
so the two never interleave in a pipe.

What reaches a log line, and how it is protected (OWASP ASVS 5.0 V16):

* each detailed entry carries a UTC timestamp, level and module (16.2.1, 16.2.2)
* control characters in messages are escaped, so text scraped from a website
  cannot forge extra log lines (16.4.1, CWE-117)
* registered secrets - API keys and tokens - are masked wherever they appear,
  tracebacks included (16.2.5); passwords and cookies are never passed to a
  logger at all
* a log file is created readable by its owner only (16.4.2)

Nothing is configured on import. `applicant` carries a NullHandler, so importing
any of this from another program is silent until that program asks otherwise;
`configure()` is called by `main()` and by anyone embedding the library who
wants the commentary.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import TextIO

ROOT = 'applicant'
MASK = '***'

QUIET, NORMAL = -1, 0

# One file per run, named for when the run started. In a directory of their own
# rather than beside job_listing.json and applied_jobs.csv, because those two are
# one stable file each and these accumulate - a working directory that gains a
# file every time you run anything is one nobody keeps working in.
FOLDER = 'logs'
FILENAME = 'run_{}.log'
STAMP = '%Y%m%d-%H%M%S'

# libraries whose records go through the same handlers, so they are escaped and
# redacted too; httpx logs every request at INFO, which is only wanted at -vv
THIRD_PARTY = ('httpx', 'httpcore')

# C0 controls, DEL and the C1 controls, minus tab: newlines are what forge a
# log line, and ESC or the 8-bit CSI (\x9b) what rewrites a terminal
# - and the Unicode line and paragraph separators, which split a line for
# anything reading the file with str.splitlines() as surely as a newline
CONTROL = re.compile(r'[\x00-\x08\x0a-\x1f\x7f-\x9f\u2028\u2029]')

_secrets: set[str] = set()

logging.getLogger(ROOT).addHandler(logging.NullHandler())


# -- what may reach a line ------------------------------------------------


def register_secret(value: str | None) -> None:
    """Mask this value in every log line from here on. Short values are ignored,
    since masking a two letter string would shred ordinary words."""
    if value and len(value) >= 8:
        _secrets.add(value)


def escape(text: str) -> str:
    """'a\\nb' -> 'a\\\\x0ab': one entry stays one line whatever it contains."""
    return CONTROL.sub(lambda match: '\\x{:02x}'.format(ord(match.group())), text)


def redact(text: str) -> str:
    for secret in _secrets:
        text = text.replace(secret, MASK)
    return text


class SafeFormatter(logging.Formatter):
    """Escapes and redacts the message; UTC timestamps with an explicit Z.

    The default layout is the detailed one, for files and -v. `fmt='%(message)s'`
    gives the plain console layout, which is protected exactly the same way.
    """

    converter = time.gmtime

    def __init__(self, fmt: str = '%(asctime)s %(levelname)s %(name)s: %(message)s'):
        super().__init__(fmt, '%Y-%m-%dT%H:%M:%SZ')

    def format(self, record: logging.LogRecord) -> str:
        record = logging.makeLogRecord(record.__dict__)
        record.msg = escape(redact(record.getMessage()))
        record.args = None
        text = super().format(record)
        # tracebacks keep their newlines, but a secret inside one is still masked
        return redact(text)


# INFO is the running commentary and reads as it always did, so a normal run
# looks the same as it did when these were print calls. -v adds where each line
# came from and when, which is what you want when a board starts misbehaving.
PLAIN = SafeFormatter('%(message)s')
DETAILED = SafeFormatter()


# -- where it goes ----------------------------------------------------------


class _Stderr(logging.StreamHandler):
    """Whatever sys.stderr is *now*, not whatever it was when configured.

    A test redirecting stderr, or a program swapping it, would otherwise leave
    the handler writing into a stream nobody reads any more.
    """

    def __init__(self):
        super().__init__(sys.stderr)

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value):
        del value


class _OwnerOnlyFile(logging.FileHandler):
    """A log file created 0600, and only once there is something to put in it.

    Logs can hold company names, urls and error detail that are nobody else's
    business (ASVS 16.4.2), so the file is created with the mode rather than
    chmod-ed afterwards, which would leave a window where it is world readable.
    """

    def _open(self):
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, self.mode, encoding=self.encoding, errors=self.errors)


def default_file(now: datetime | None = None, folder: str = FOLDER) -> str:
    """`logs/run_20260812-143502.log`.

    UTC, like the timestamps inside the file - one feature, one clock. Seconds
    are enough to keep two runs apart; nobody starts two of these in the same
    second.
    """
    moment = now or datetime.now(timezone.utc)
    return os.path.join(folder, FILENAME.format(moment.strftime(STAMP)))


def get(name: str) -> logging.Logger:
    """The logger for a module: `get(__name__)` inside the package."""
    if name == ROOT or name.startswith(ROOT + '.'):
        return logging.getLogger(name)
    return logging.getLogger('{}.{}'.format(ROOT, name))


def level_for(verbosity: int) -> int:
    """-1 and below is quiet, 0 is the usual commentary, 1 and up is everything."""
    if verbosity <= QUIET:
        return logging.WARNING
    return logging.INFO if verbosity == NORMAL else logging.DEBUG


# marks the handlers configure() added, so undoing it leaves everyone else's -
# our NullHandler, or whatever an embedding program put on httpx - in place
OURS = '_applicant_handler'


def _detach(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, OURS, False):
            logger.removeHandler(handler)
            handler.close()


def configure(
    verbosity: int = NORMAL,
    stream: TextIO | None = None,
    filepath: str | None = None,
) -> logging.Logger:
    """Send the library's commentary somewhere, and say how much of it.

    The console is stderr unless `stream` says otherwise. Idempotent: calling it
    again replaces the handlers it added rather than doubling every line, which
    matters because a test suite calls `main()` dozens of times in one process.

    A log file always gets the detailed format and everything down to DEBUG,
    whatever the console is showing - the point of a file is to hold what you
    did not know you would want.
    """
    level = level_for(verbosity)

    console: logging.Handler = logging.StreamHandler(stream) if stream is not None else _Stderr()
    console.setLevel(level)
    console.setFormatter(DETAILED if level <= logging.DEBUG else PLAIN)
    handlers: list[logging.Handler] = [console]

    if filepath:
        # The directory is made here rather than left to the handler: `delay`
        # defers opening the file until the first record, and a path that cannot
        # be written should fail now, while it can still be corrected, rather
        # than halfway through a scrape. Making it also means `logs/` exists
        # without anyone having to create it first.
        folder = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(folder, exist_ok=True)
        # ...and the file itself, which `delay` would otherwise first open on
        # the first record - outside the caller's handling of a bad path
        if os.path.isdir(filepath):
            raise IsADirectoryError(errno.EISDIR, 'is a directory', filepath)
        if not os.access(filepath if os.path.exists(filepath) else folder, os.W_OK):
            raise PermissionError(errno.EACCES, 'not writable', filepath)

        # delay=True so a command that says nothing leaves no file behind: with
        # a file written on every run, an empty one is just litter
        record = _OwnerOnlyFile(filepath, encoding='utf-8', delay=True)
        record.setLevel(logging.DEBUG)
        record.setFormatter(DETAILED)
        handlers.append(record)

    # httpx's request lines only at -vv; our own records down to DEBUG whenever
    # a file is there to take them
    third_party = logging.DEBUG if verbosity >= 2 else logging.WARNING
    for handler in handlers:
        setattr(handler, OURS, True)
    for name, threshold in (
        (ROOT, logging.DEBUG if filepath else level),
        *((name, third_party) for name in THIRD_PARTY),
    ):
        logger = logging.getLogger(name)
        _detach(logger)
        for handler in handlers:
            logger.addHandler(handler)
        logger.setLevel(threshold)
        # Only once we are handling the output ourselves. Until `configure` is
        # called these records propagate normally, so a program that embeds the
        # library and configures its own root logger sees them; after it, they
        # do not, so a program that does both is not shown every line twice.
        logger.propagate = False

    return logging.getLogger(ROOT)


def silence() -> None:
    """Undo `configure`, all of it: back to what a freshly imported library is.

    Including propagation, or a caller who configured us once could never hand
    the records back to their own logging again.
    """
    for name in (ROOT, *THIRD_PARTY):
        logger = logging.getLogger(name)
        _detach(logger)
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
