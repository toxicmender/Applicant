"""The command line: argument parsing and the handlers behind each subcommand.

Importing this module does nothing. `main(argv)` builds the parser, dispatches,
and returns an exit code, so every path here is reachable from a test.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from pathlib import Path

from . import logs
from .search import SOURCES

logger = logging.getLogger(__name__)

REVIEW_SOURCES = ('ambitionbox', 'glassdoor')
FINANCIAL_SOURCES = ('crunchbase', 'tracxn')


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
        logger.info(f'jobs: restoring the LinkedIn session from {fp}')
        operator.restore_session(fp)
    else:
        # the credentials go to the browser and nowhere else - never to a log
        logger.info(f'jobs: signing in to LinkedIn, session to be saved at {fp}')
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
        logger.info(f'reviews: fetching {args.company!r} from {source}')
        try:
            rating = client.fetch(args.company, max_reviews=args.max_reviews)
        except ReviewsError as error:
            # one blocked source should not throw away the other one's results
            logger.warning(f'reviews: {source} failed: {type(error).__name__}: {error}')
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
        logger.error(f'reviews: no source returned a rating for {args.company!r}')
        return 1

    with open(args.output, 'w', encoding='utf-8') as file:
        json.dump(results if len(results) > 1 else results[0], file, indent=2, ensure_ascii=False)
    logger.info(f'reviews: {len(results)} rating(s) written to {args.output}')
    print('written to {}'.format(args.output))
    return 0


def _companies_from_jobs(path: str) -> list[str]:
    """Every distinct company in a job listing file, first spelling wins."""
    from .storage import load_jobs

    seen: dict[str, str] = {}
    for job in load_jobs(path):
        name = (job.company or '').strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


def _financials_clients(args) -> dict:
    """One place that knows which client a --source names."""
    from .financials import CrunchbaseClient, TracxnClient

    wanted = FINANCIAL_SOURCES if args.source == 'both' else (args.source,)
    clients = {}
    if 'crunchbase' in wanted:
        clients['crunchbase'] = CrunchbaseClient(
            api_key=args.crunchbase_key,
            profile_dir=args.cb_profile,
            login=args.login,
            headless=not args.show,
        )
    if 'tracxn' in wanted:
        clients['tracxn'] = TracxnClient(
            api_key=args.tracxn_key,
            profile_dir=args.tx_profile,
            login=args.login,
            headless=not args.show,
        )
    return clients


def run_financials(args) -> int:
    import time

    from .financials import FinancialsError, FinancialsTracker
    from .financials.tracker import describe_round

    companies = list(args.companies)
    if args.from_jobs:
        companies += [
            name for name in _companies_from_jobs(args.from_jobs) if name not in companies
        ]
    if not companies:
        print('name at least one company, or pass --from-jobs job_listing.json')
        return 1
    logger.info(f'financials: tracking {len(companies)} company(ies) in {args.output}')

    clients = _financials_clients(args)
    tracker = FinancialsTracker(args.output)
    found = 0
    for index, company in enumerate(companies):
        if index:
            time.sleep(1)  # both sources rate limit, and the API ones bill per call
        for source, client in clients.items():
            # a profile url belongs to its own site; the other one cannot use it
            other = 'tracxn.com' if source == 'crunchbase' else 'crunchbase.com'
            if other in company.lower():
                continue
            logger.info(f'financials: fetching {company!r} from {source}')
            try:
                financials = client.fetch(company, max_rounds=args.max_rounds)
            except FinancialsError as error:
                # one blocked source should not throw away the other one's results
                logger.warning(
                    f'financials: {source} failed for {company!r}: {type(error).__name__}: {error}'
                )
                print('{}: {}: {}'.format(source, company, error))
                continue

            changes = tracker.record(financials)
            logger.info(f'financials: {source} {company!r}: {len(changes)} change(s) recorded')
            found += 1
            print('{}: {} - {}'.format(source, financials.company, financials.summary()))
            for change in changes:
                print('  changed: {}'.format(change))
            for note in financials.notes:
                print('  note: {}'.format(note))
            if args.rounds:
                for item in financials.rounds:
                    print('  - {}'.format(describe_round(item)))

    if not found:
        logger.error(f'financials: nothing fetched for {len(companies)} company(ies)')
        return 1
    print('tracked in {}'.format(args.output))
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
        keep_unknown=not args.strict,
    )


def run_search(args) -> int:
    from .search import SOURCES as ALL
    from .search import Jobs
    from .storage import save_jobs

    wanted = ALL if 'all' in args.source else args.source
    logger.info(f'search: {args.keywords!r} on {", ".join(wanted)}, up to {args.limit} per board')
    board = Jobs(sources=wanted, headless=not args.show)
    found = board.search(args.keywords, _filters(args), limit=args.limit)

    if not found:
        logger.info('search: no job survived the filters')
        print('nothing matched')
        return 1

    total = save_jobs(found, args.output)
    logger.info(f'search: {len(found)} job(s) saved, {total} now in {args.output}')
    print('{} jobs written to {} ({} stored in total)'.format(len(found), args.output, total))
    return 0


def run_apply(args) -> int:
    from .search import Jobs
    from .storage import load_jobs

    jobs = load_jobs(args.input)
    if not jobs:
        logger.warning(f'apply: no jobs could be read from {args.input}')
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

    logger.info(
        f'apply: {len(matching)} job(s) to {args.log}' + (' (dry run)' if args.dry_run else '')
    )
    Jobs(headless=not args.show).apply(matching, log=args.log, dry_run=args.dry_run)
    return 0


def run_status(args) -> int:
    """What is stored right now: jobs found, and what came of them."""
    from .storage import ApplicationLog, load_jobs

    jobs = load_jobs(args.input)
    counts = ApplicationLog(args.log).counts()
    logger.info(f'status: {len(jobs)} job(s) in {args.input}, log {args.log}')

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
        logger.info(f'status: summary written to {args.json}')
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
        logger.info(
            f'rates: {len(updated)} updated, {len(failed)} failed, {len(skipped)} already known'
        )
        if failed:
            logger.warning(f'rates: no PPP factor returned for {", ".join(sorted(failed))}')
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
    command.add_argument('-t', '--title', help='keep jobs whose title contains these words')
    command.add_argument('-c', '--company', help='keep jobs from companies matching this')
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
        help='drop jobs whose salary or date could not be read '
        '(they are kept and flagged by default)',
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='applicant',
        description='Scrape and apply to jobs, and look up company ratings and financials',
    )
    commands = parser.add_subparsers(dest='command')

    # on every subcommand, so they go where people type them: `applicant search x -v`
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        '-v',
        '--verbose',
        action='count',
        default=0,
        help='log progress to stderr; -vv adds debug detail and HTTP requests',
    )
    common.add_argument('-q', '--quiet', action='store_true', help='log errors only')
    common.add_argument(
        '--log-file', metavar='PATH', help='also write a debug log here (created owner-only)'
    )

    def add(name: str, **options) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[common], **options)

    jobs = add('jobs', help='scrape LinkedIn jobs while signed in')
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

    search = add('search', help='search job boards without signing in')
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
        '-o', '--output', default='job_listing.json', help='file path to store the scraped jobs'
    )
    search.add_argument(
        '--show', action='store_true', help='run the browser visibly, to solve a bot check yourself'
    )
    _add_filters(search)
    search.set_defaults(handler=run_search)

    apply_ = add('apply', help='apply to stored jobs and log them for a spreadsheet')
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

    reviews = add('reviews', help='fetch company ratings and pros/cons')
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

    financials = add(
        'financials',
        help='track company funding, valuation and revenue from Crunchbase and Tracxn',
    )
    financials.add_argument(
        'companies',
        nargs='*',
        help='company names, Crunchbase permalinks/urls, or Tracxn domains/ids/profile urls',
    )
    financials.add_argument(
        '--from-jobs',
        metavar='PATH',
        help='also track every company in a scraped jobs file, e.g. job_listing.json',
    )
    financials.add_argument(
        '-s',
        '--source',
        default='both',
        choices=[*FINANCIAL_SOURCES, 'both'],
        help='where to read financials from',
    )
    financials.add_argument(
        '-n', '--max-rounds', type=int, default=20, help='funding rounds to pull per company'
    )
    financials.add_argument(
        '-o',
        '--output',
        default='company_financials.json',
        help='file that keeps the history and is compared against',
    )
    financials.add_argument('--rounds', action='store_true', help='print each funding round too')
    financials.add_argument(
        '--crunchbase-key',
        help='Crunchbase API key (default: $CRUNCHBASE_API_KEY); without one the website is read',
    )
    financials.add_argument(
        '--tracxn-key',
        help='Tracxn API token (default: $TRACXN_API_KEY); without one a profile url is needed',
    )
    financials.add_argument(
        '--login',
        action='store_true',
        help='open a visible browser so you can clear the bot check and sign in',
    )
    financials.add_argument('--show', action='store_true', help='run the browser visibly')
    financials.add_argument(
        '--cb-profile',
        default='.cb_profile',
        help='directory holding the reused Crunchbase browser profile',
    )
    financials.add_argument(
        '--tx-profile',
        default='.tx_profile',
        help='directory holding the reused Tracxn browser profile',
    )
    financials.set_defaults(handler=run_financials)

    rates = add('rates', help='show or refresh the locally cached PPP conversion factors')
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

    status = add('status', help='summarise stored jobs and applications')
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

    logs.setup(-1 if args.quiet else args.verbose, args.log_file)
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
    except Exception as error:
        logger.critical(
            f'{args.command} failed unexpectedly: {type(error).__name__}: {error} '
            '- rerun with -vv or --log-file for the traceback'
        )
        logger.debug(f'{args.command}: traceback', exc_info=True)
        return 1


# argparse attributes that are plumbing, or secret, rather than something to log
NOT_LOGGED = {'handler', 'command', 'verbose', 'quiet', 'log_file'}
SECRET_ARGS = {'crunchbase_key', 'tracxn_key'}


def _loggable(args) -> dict:
    """The options a run was given, with keys masked, for the debug log."""
    shown = {}
    for key, value in sorted(vars(args).items()):
        if key in NOT_LOGGED:
            continue
        if key in SECRET_ARGS:
            logs.register_secret(value)
            value = logs.MASK if value else None
        shown[key] = value
    return shown
