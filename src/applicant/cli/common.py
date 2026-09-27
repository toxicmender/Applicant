"""What several commands share: the logging and filter flags, and a filter built from them."""

from __future__ import annotations

import argparse

from ..filters import JobFilter


def shared_flags(*, suppress: bool) -> argparse.ArgumentParser:
    """The flags every command takes, as a parent parser.

    Used twice: by the top-level parser, so `applicant -v search x` works, and
    by every subcommand, so `applicant search x -v` does too. The subcommands'
    copies default to SUPPRESS - argparse copies a subcommand's namespace over
    the top-level one, so a real default there would overwrite a flag given
    before the subcommand.
    """

    def default(value):
        return argparse.SUPPRESS if suppress else value

    flags = argparse.ArgumentParser(add_help=False)
    flags.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=default(0),
        help='say more about what each board is doing, with timestamps; '
        '-vv also logs each HTTP request',
    )
    flags.add_argument(
        '-q',
        '--quiet',
        action='store_true',
        default=default(False),
        help='only warnings and failures',
    )
    flags.add_argument(
        '--log-file',
        metavar='PATH',
        default=default(None),
        help='write everything, in full detail, to this file. Defaults to '
        'logs/run_<timestamp>.log, and any directory named is created',
    )
    flags.add_argument(
        '--no-log-file',
        action='store_true',
        default=default(False),
        help='do not write a log file for this run',
    )
    flags.add_argument(
        '--data-dir',
        metavar='DIR',
        default=default(None),
        help='where applicant keeps its files and database; relative file names '
        'go here (default: the current directory, or $APPLICANT_HOME)',
    )
    flags.add_argument(
        '--store',
        choices=['sqlite', 'files'],
        default=default(None),
        help='sqlite (default): applicant.db beside the files is the record and the '
        'JSON/CSV are exports; files: the JSON/CSV alone',
    )
    flags.add_argument(
        '--config',
        metavar='PATH',
        default=default(None),
        help='settings file to read (default: applicant.toml here, if there is one)',
    )
    return flags


def add_filters(command) -> None:
    command.add_argument(
        '-t',
        '--title',
        action='append',
        help='keep jobs whose title contains these words. Repeat it to accept '
        'any of several: -t ai -t ml -t "machine learning"',
    )
    command.add_argument(
        '-c',
        '--company',
        action='append',
        help='keep jobs from companies matching this. Repeatable, like --title',
    )
    command.add_argument('--min-salary', type=float, help='annual salary floor, in --currency')
    command.add_argument('--currency', help='currency for --min-salary, e.g. INR or USD')
    command.add_argument(
        '--salary-basis',
        default='ppp',
        choices=['ppp', 'market', 'strict'],
        help='how to compare pay in another currency: purchasing power (default), '
        'the exchange rate, or not at all',
    )
    command.add_argument(
        '-e',
        '--experience',
        type=float,
        metavar='YEARS',
        help='years of experience you have; keeps jobs asking for it',
    )
    command.add_argument(
        '--posted-within', type=int, metavar='DAYS', help='keep jobs posted within this many days'
    )
    command.add_argument(
        '--strict',
        action='store_true',
        help='drop jobs whose salary, experience or date could not be read '
        '(they are kept and flagged by default). Note that this drops every '
        'board that does not publish the field at all',
    )
    command.add_argument(
        '--strict-published',
        action='store_true',
        help='drop a job only when its board does publish the field and the '
        'posting stayed silent, keeping boards that never publish it - which '
        'for --experience is every board but Naukri',
    )


def filters_from(args) -> JobFilter:
    """The JobFilter the filter flags describe."""
    return JobFilter(
        title=args.title,
        company=args.company,
        location=args.location,
        min_salary=args.min_salary,
        currency=args.currency,
        salary_basis=args.salary_basis,
        experience=args.experience,
        posted_within_days=args.posted_within,
        keep_unknown=not (args.strict or args.strict_published),
        # --strict is the stricter of the two, so it wins when both are given
        keep_unpublished=args.strict_published and not args.strict,
    )
