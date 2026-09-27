"""What several commands share: the logging and filter flags, and a filter built from them."""

from __future__ import annotations

from ..filters import JobFilter


def add_logging(command) -> None:
    """On every subcommand rather than before them.

    A global flag would have to come first - `applicant -v search ...` - and
    `normalise` reads a leading flag as the old bare-flag invocation of `jobs`,
    so it would be rewritten into nonsense.
    """
    command.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='say more about what each board is doing, with timestamps; '
        '-vv also logs each HTTP request',
    )
    command.add_argument('-q', '--quiet', action='store_true', help='only warnings and failures')
    command.add_argument(
        '--log-file',
        metavar='PATH',
        help='write everything, in full detail, to this file. Defaults to '
        'logs/run_<timestamp>.log, and any directory named is created',
    )
    command.add_argument(
        '--no-log-file',
        action='store_true',
        help='do not write a log file for this run',
    )


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
