"""Where the tool's running commentary goes.

A scrape talks while it works - which board answered, how many postings
survived, which one refused - and until now it said all of it with `print`.
That is fine for one run in a terminal and no use at all for the two things
people actually do with a long scrape: turn the noise down, or keep a record of
what happened while they were not watching.

So the library logs and the commands print. A message *about* the work in
progress goes to a logger here; the answer a subcommand was asked for -
`status`' tally, `rates`' table, where a file was written - stays a `print`,
because that is the command's output rather than its commentary, and silencing
it with `--quiet` would be silencing the answer.

Nothing is configured on import. `applicant` carries a NullHandler, so importing
any of this from another program is silent until that program asks otherwise;
`configure()` is called by `main()` and by anyone embedding the library who
wants the commentary.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from typing import TextIO

ROOT = 'applicant'

# INFO is the running commentary and reads as it always did, so a normal run
# looks the same as it did when these were print calls. -v adds where each line
# came from and when, which is what you want when a board starts misbehaving.
PLAIN = logging.Formatter('%(message)s')
DETAILED = logging.Formatter('%(asctime)s %(levelname)-7s %(name)s: %(message)s', '%H:%M:%S')

QUIET, NORMAL = -1, 0

# One file per run, named for when the run started, in the directory the command
# was invoked from - beside job_listing.json and applied_jobs.csv, which is
# where this tool already keeps what it produces.
FILENAME = 'run_{}.log'
STAMP = '%Y%m%d-%H%M%S'

logging.getLogger(ROOT).addHandler(logging.NullHandler())


def default_file(now: datetime | None = None) -> str:
    """`run_20260812-143502.log`. Local time, like the timestamps inside it."""
    return FILENAME.format((now or datetime.now()).strftime(STAMP))


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


def configure(
    verbosity: int = NORMAL,
    stream: TextIO | None = None,
    filepath: str | None = None,
) -> logging.Logger:
    """Send the library's commentary somewhere, and say how much of it.

    Idempotent: calling it again replaces the handlers it added rather than
    doubling every line, which matters because a test suite calls `main()`
    dozens of times in one process.

    A log file always gets the detailed format and everything down to DEBUG,
    whatever the console is showing - the point of a file is to hold what you
    did not know you would want.
    """
    logger = logging.getLogger(ROOT)
    level = level_for(verbosity)
    logger.setLevel(logging.DEBUG if filepath else level)
    # Only once we are handling the output ourselves. Until `configure` is
    # called these records propagate normally, so a program that embeds the
    # library and configures its own root logger sees them; after it, they do
    # not, so a program that does both is not shown every line twice.
    logger.propagate = False

    for handler in list(logger.handlers):
        if not isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)
            handler.close()

    console = logging.StreamHandler(stream if stream is not None else sys.stdout)
    console.setLevel(level)
    console.setFormatter(DETAILED if level <= logging.DEBUG else PLAIN)
    logger.addHandler(console)

    if filepath:
        # delay defers opening until the first record, so the directory is
        # checked here instead - a mistyped path should fail now, while it can
        # still be corrected, rather than halfway through a scrape
        folder = os.path.dirname(os.path.abspath(filepath))
        if not os.path.isdir(folder):
            raise FileNotFoundError('no directory {} to write {} into'.format(folder, filepath))

        # delay=True so a command that says nothing leaves no file behind: with
        # a file written on every run, an empty one is just litter
        record = logging.FileHandler(filepath, encoding='utf-8', delay=True)
        record.setLevel(logging.DEBUG)
        record.setFormatter(DETAILED)
        logger.addHandler(record)

    return logger


def silence() -> None:
    """Undo `configure`, all of it: back to what a freshly imported library is.

    Including propagation, or a caller who configured us once could never hand
    the records back to their own logging again.
    """
    logger = logging.getLogger(ROOT)
    for handler in list(logger.handlers):
        if not isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
