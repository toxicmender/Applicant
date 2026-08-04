"""The command line: argument parsing and the handlers behind each subcommand.

Importing this module does nothing. `main(argv)` builds the parser, dispatches,
and returns an exit code, so every path here is reachable from a test.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from .search import SOURCES

REVIEW_SOURCES = ('ambitionbox', 'glassdoor')


def run_jobs(args) -> int:
    from .boards.linkedin import LinkedIn

    if args.driver != 'chromedriver':
        print(
            'note: --driver is ignored now that LinkedIn runs on Playwright, '
            'which manages its own browser'
        )

    operator = LinkedIn(path=args.driver, headless=args.Display)

    fp = Path(args.cookies)

    if fp.exists() and not fp.is_dir() and not args.overwrite:
        operator.restore_session(fp)
    else:
        user = input('Username/Email ID: ')
        passw = getpass.getpass()
        operator.login(
            username=user, password=passw, twoFA=args.twofa, filepath=fp, overwrite=args.overwrite
        )

    operator.scrape_jobs(args.jobs)
    return 0


def _review_client(source: str, args):
    """One place that knows which client a --source names."""
    from .reviews import AmbitionBoxClient, GlassdoorClient

    if source == 'ambitionbox':
        return AmbitionBoxClient()
    return GlassdoorClient(profile_dir=args.profile, login=args.login, headless=args.Display)


def run_reviews(args) -> int:
    from .reviews import ReviewsError

    sources = list(REVIEW_SOURCES) if args.source == 'both' else [args.source]
    results = []

    for source in sources:
        client = _review_client(source, args)
        try:
            rating = client.fetch(args.company, max_reviews=args.max_reviews)
        except ReviewsError as error:
            # one blocked source should not throw away the other one's results
            print('{}: {}'.format(source, error))
            continue

        print(
            '{}: {} - {} out of 5 from {} ratings ({} reviews fetched)'.format(
                source,
                rating.company,
                rating.overall_rating,
                rating.review_count,
                len(rating.reviews),
            )
        )
        results.append(rating.to_dict())

    if not results:
        return 1

    with open(args.output, 'w', encoding='utf-8') as file:
        json.dump(results if len(results) > 1 else results[0], file, indent=2, ensure_ascii=False)
    print('written to {}'.format(args.output))
    return 0


def _filters(args):
    from .filters import JobFilter

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


def run_search(args) -> int:
    from .search import SOURCES as ALL
    from .search import Jobs
    from .storage import save_jobs

    wanted = ALL if 'all' in args.source else args.source
    board = Jobs(sources=wanted, headless=not args.show)
    found = board.search(
        args.keywords,
        _filters(args),
        limit=args.limit,
        want=args.want,
        max_rounds=args.max_rounds,
    )

    if not found:
        print('nothing matched')
        return 1

    total = save_jobs(found, args.output)
    print('{} jobs written to {} ({} stored in total)'.format(len(found), args.output, total))
    return 0


def run_apply(args) -> int:
    from .search import Jobs
    from .storage import load_jobs

    jobs = load_jobs(args.input)
    if not jobs:
        print('no jobs to apply to in {}'.format(args.input))
        return 1

    filters = _filters(args)
    matching = []
    for job in jobs:
        keep, flags = filters.matches(job)
        if keep:
            job.flags = flags
            matching.append(job)

    print('{} of {} stored jobs match'.format(len(matching), len(jobs)))
    if not matching:
        return 1

    Jobs(headless=not args.show).apply(matching, log=args.log, dry_run=args.dry_run)
    return 0


def run_status(args) -> int:
    """What is stored right now: jobs found, and what came of them."""
    from .storage import ApplicationLog, load_jobs

    jobs = load_jobs(args.input)
    counts = ApplicationLog(args.log).counts()

    by_source: dict[str, int] = {}
    for job in jobs:
        by_source[job.source] = by_source.get(job.source, 0) + 1

    print('{} jobs stored in {}'.format(len(jobs), args.input))
    for source in sorted(by_source):
        print('  {}: {}'.format(source, by_source[source]))

    total = sum(counts.values())
    print('{} applications recorded in {}'.format(total, args.log))
    for status in sorted(counts):
        print('  {}: {}'.format(status, counts[status]))

    if args.json:
        payload = {
            'input': args.input,
            'log': args.log,
            'jobs': {'total': len(jobs), 'by_source': by_source},
            'applications': {'total': total, 'by_status': counts},
        }
        with open(args.json, 'w', encoding='utf-8') as file:
            json.dump(payload, file, indent=2)
        print('written to {}'.format(args.json))

    return 0


def run_rates(args) -> int:
    """Show, or top up, the locally cached PPP factors."""
    from .money import CURRENCY_COUNTRY, load_factors, refresh_factors

    if args.refresh:
        wanted = args.currency or None
        print(
            'fetching PPP factors from the World Bank{}...'.format(
                '' if wanted is None else ' for ' + ', '.join(c.upper() for c in wanted)
            )
        )
        print('one country at a time - the API throttles hard, so this is not quick.\n')

        def report(country, found, was_skipped):
            if was_skipped:
                return
            if found is None:
                print('  {}: no value returned'.format(country))
            else:
                print('  {}: {} ({})'.format(country, found['value'], found['year']))

        updated, failed, skipped = refresh_factors(
            currencies=wanted, force=args.force, on_result=report
        )
        print(
            '\n{} updated, {} failed, {} already known'.format(
                len(updated), len(failed), len(skipped)
            )
        )
        if failed:
            print('not recorded (nothing is written from memory): ' + ', '.join(sorted(failed)))
        if updated:
            print('written to ppp_factors.json - commit it so others start with them')

    factors = load_factors()
    known = sum(1 for country in CURRENCY_COUNTRY.values() if country in factors)
    print(
        '\n{} of {} mapped currencies have a local PPP factor'.format(
            known, len(set(CURRENCY_COUNTRY.values()))
        )
    )

    for currency, country in sorted(CURRENCY_COUNTRY.items()):
        entry = factors.get(country)
        if entry is None:
            print('  {} ({}): not cached - fetched on demand'.format(currency, country))
        else:
            print(
                '  {} ({}): {} per international $ ({})'.format(
                    currency, country, entry['value'], entry.get('year', '?')
                )
            )

    return 0


def _add_filters(command) -> None:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='applicant', description='Scrape and apply to jobs, and look up company ratings'
    )
    commands = parser.add_subparsers(dest='command')

    jobs = commands.add_parser('jobs', help='scrape LinkedIn jobs while signed in')
    jobs.add_argument(
        '-c',
        '--cookies',
        default='cookies.json',
        help='file path to where cookies are or to store them',
    )
    jobs.add_argument(
        '-w',
        '--overwrite',
        action='store_true',
        help='overwrite existing session if it already exists',
    )
    jobs.add_argument(
        '-d',
        '--driver',
        default='chromedriver',
        help='accepted for compatibility; Playwright manages its own browser',
    )
    jobs.add_argument(
        '-t',
        '--twofa',
        action='store_true',
        help='use it if you have 2 Factor Authentication enabled',
    )
    jobs.add_argument(
        '-j',
        '--jobs',
        default='job_listing.json',
        help='file path to where jobs urls are or to store them',
    )
    jobs.add_argument(
        '-D',
        '--Display',
        action='store_false',
        help='Whether to display the browser or not (headless mode)',
    )
    jobs.set_defaults(handler=run_jobs)

    search = commands.add_parser('search', help='search job boards without signing in')
    search.add_argument('keywords', help='what to search for, e.g. "python developer"')
    search.add_argument('-l', '--location', default='', help='where to search')
    search.add_argument(
        '-s',
        '--source',
        default=['all'],
        nargs='+',
        choices=[*SOURCES, 'all'],
        help='which boards to search',
    )
    search.add_argument(
        '-n', '--limit', type=int, default=25, help='jobs to pull per board, before filtering'
    )
    search.add_argument(
        '--want',
        type=int,
        metavar='N',
        help='keep reading each board until this many jobs survive the filter, '
        'rather than filtering a fixed pull of --limit',
    )
    search.add_argument(
        '--max-rounds',
        type=int,
        default=4,
        metavar='N',
        help='how many times --want may re-read a board, each round doubling the '
        'pull and re-reading what it already saw (default 4)',
    )
    search.add_argument(
        '-o', '--output', default='job_listing.json', help='file path to store the scraped jobs'
    )
    search.add_argument(
        '--show', action='store_true', help='run the browser visibly, to solve a bot check yourself'
    )
    _add_filters(search)
    search.set_defaults(handler=run_search)

    apply_ = commands.add_parser(
        'apply', help='apply to stored jobs and log them for a spreadsheet'
    )
    apply_.add_argument(
        '-i', '--input', default='job_listing.json', help='file path to the scraped jobs'
    )
    apply_.add_argument('--log', default='applied_jobs.csv', help='CSV to append applications to')
    apply_.add_argument(
        '-l', '--location', default=None, help='only apply to jobs in this location'
    )
    apply_.add_argument(
        '--dry-run', action='store_true', help='show what would be applied to, without applying'
    )
    apply_.add_argument('--show', action='store_true', help='run the browser visibly')
    _add_filters(apply_)
    apply_.set_defaults(handler=run_apply)

    reviews = commands.add_parser('reviews', help='fetch company ratings and pros/cons')
    reviews.add_argument(
        'company',
        help='AmbitionBox slug (e.g. tcs), or a Glassdoor reviews url / '
        'slug with employer id (e.g. Google-E9079)',
    )
    reviews.add_argument(
        '-s',
        '--source',
        default='ambitionbox',
        choices=[*REVIEW_SOURCES, 'both'],
        help='where to read ratings from',
    )
    reviews.add_argument(
        '-n', '--max-reviews', type=int, default=20, help='how many individual reviews to pull'
    )
    reviews.add_argument(
        '-o',
        '--output',
        default='company_reviews.json',
        help='file path to store the scraped ratings',
    )
    reviews.add_argument(
        '--login',
        action='store_true',
        help='open a visible browser so you can clear the Glassdoor bot check and sign in',
    )
    reviews.add_argument(
        '--profile',
        default='.gd_profile',
        help='directory holding the reused Glassdoor browser profile',
    )
    reviews.add_argument(
        '-D',
        '--Display',
        action='store_false',
        help='Whether to display the browser or not (headless mode)',
    )
    reviews.set_defaults(handler=run_reviews)

    rates = commands.add_parser(
        'rates', help='show or refresh the locally cached PPP conversion factors'
    )
    rates.add_argument(
        '--refresh',
        action='store_true',
        help='fetch missing factors from the World Bank and record them',
    )
    rates.add_argument(
        '--force', action='store_true', help='re-fetch factors that are already recorded'
    )
    rates.add_argument(
        '-c',
        '--currency',
        nargs='+',
        metavar='CODE',
        help='limit the refresh to these currencies, e.g. GBP SEK NZD',
    )
    rates.set_defaults(handler=run_rates)

    status = commands.add_parser('status', help='summarise stored jobs and applications')
    status.add_argument(
        '-i', '--input', default='job_listing.json', help='file path to the scraped jobs'
    )
    status.add_argument(
        '--log', default='applied_jobs.csv', help='CSV the applications were appended to'
    )
    status.add_argument('--json', help='also write the summary to this file as JSON')
    status.set_defaults(handler=run_status)

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
    return handler(args) or 0
