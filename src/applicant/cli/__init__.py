"""The command line: argument parsing, and one module per subcommand.

Importing this package does nothing. `main(argv)` builds the parser, dispatches,
and returns an exit code, so every path here is reachable from a test. Each
subcommand's module declares its flags (`add_parser`) and does its work
(`run`) by calling a service in `applicant.services` and printing the answer;
none of them holds the logic itself.

Handlers are bound here, through this module's own names, so a test can replace
`applicant.cli.run_status` and have the parser dispatch to the replacement.

Exit codes: 0 done, 1 nothing found or an unexpected failure, 2 a usage error,
3 blocked by a bot check or rate limit, 4 not found, 5 a page that could not
be read, 6 a key refused or out of credits, 130 interrupted.
"""

from __future__ import annotations

import argparse
import sys

from .. import log
from ..errors import AuthFailed, Blocked, NotFound, SourceError, Unparseable
from ..log import QUIET, configure, default_file, get
from . import apply, financials, jobs, rates, reviews, search, status
from .common import add_filters, add_logging, filters_from

__all__ = [
    'add_filters',
    'add_logging',
    'build_parser',
    'filters_from',
    'main',
    'normalise',
    'run_apply',
    'run_financials',
    'run_jobs',
    'run_rates',
    'run_reviews',
    'run_search',
    'run_status',
]

logger = get(__name__)

run_jobs = jobs.run
run_search = search.run
run_apply = apply.run
run_reviews = reviews.run
run_financials = financials.run
run_rates = rates.run
run_status = status.run

# what an error that reached main() says about the run, for a calling script
EXIT_CODES: tuple[tuple[type[BaseException], int], ...] = (
    (Blocked, 3),
    (NotFound, 4),
    (Unparseable, 5),
    (AuthFailed, 6),
)


def exit_code_for(error: BaseException) -> int:
    for kind, code in EXIT_CODES:
        if isinstance(error, kind):
            return code
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='applicant',
        description='Scrape and apply to jobs, and look up company ratings and financials',
    )
    commands = parser.add_subparsers(dest='command')

    def add(name: str, **options) -> argparse.ArgumentParser:
        return commands.add_parser(name, **options)

    # the names are read here, at call time, so a patched run_* is the one bound
    jobs.add_parser(add).set_defaults(handler=run_jobs)
    search.add_parser(add).set_defaults(handler=run_search)
    apply.add_parser(add).set_defaults(handler=run_apply)
    reviews.add_parser(add).set_defaults(handler=run_reviews)
    financials.add_parser(add).set_defaults(handler=run_financials)
    rates.add_parser(add).set_defaults(handler=run_rates)
    status.add_parser(add).set_defaults(handler=run_status)
    return parser


def normalise(argv: list[str]) -> list[str]:
    """`applicant` and `applicant -c cookies.json` still mean the LinkedIn job run.

    The README documented those before subcommands existed, so a bare invocation
    or one that opens with a flag is rewritten to `jobs`.
    """
    if not argv:
        return ['jobs']
    if argv[0].startswith('-') and argv[0] not in ('-h', '--help'):
        return ['jobs', *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(normalise(list(sys.argv[1:] if argv is None else argv)))

    handler = getattr(args, 'handler', None)
    if handler is None:
        parser.print_help()
        return 1

    # a run records itself unless told not to; the file is only created once
    # there is something to put in it
    filepath = None if args.no_log_file else (args.log_file or default_file())
    try:
        configure(verbosity=QUIET if args.quiet else args.verbose, filepath=filepath)
    except OSError as error:
        # a log file we cannot open is a mistake in the invocation, not a
        # reason to run the scrape and lose the record of it
        print('could not open {}: {}'.format(filepath, error), file=sys.stderr)
        return 2

    if filepath:
        logger.info('logging this run to {}'.format(filepath))
    logger.debug(f'{args.command}: {_loggable(args)}')

    try:
        return handler(args) or 0
    except KeyboardInterrupt:
        logger.warning(f'{args.command}: interrupted')
        return 130
    # The last resort handler (ASVS 16.5.4, CWE-248). Each source already turns
    # its expected failures into a domain error; anything reaching here is a bug
    # or an environment fault. One line on the console, the traceback in the
    # debug log - never a raw traceback in front of the user (ASVS 16.5.1).
    # A source failing is expected somewhere in any scrape, and the services
    # isolate it per source; one that still reaches here ended the whole run.
    # Its kind is the exit code, so a script can tell "blocked, retry later"
    # from "the page changed, file a bug".
    except SourceError as error:
        logger.error(f'{args.command}: {type(error).__name__}: {error}')
        return exit_code_for(error)
    except Exception as error:
        logger.critical(
            f'{args.command} failed unexpectedly: {type(error).__name__}: {error} '
            '- rerun with -v or --log-file for the traceback'
        )
        logger.debug(f'{args.command}: traceback', exc_info=True)
        return 1


# argparse attributes that are plumbing, or secret, rather than something to log
NOT_LOGGED = {'handler', 'command', 'verbose', 'quiet', 'log_file', 'no_log_file'}
SECRET_ARGS = {'crunchbase_key', 'tracxn_key'}


def _loggable(args) -> dict:
    """The options a run was given, with keys masked, for the debug log."""
    shown = {}
    for key, value in sorted(vars(args).items()):
        if key in NOT_LOGGED:
            continue
        if key in SECRET_ARGS:
            log.register_secret(value)
            value = log.MASK if value else None
        shown[key] = value
    return shown
