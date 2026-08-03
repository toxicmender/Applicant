#!/usr/bin/env python

import argparse
import getpass
import json
import sys
from pathlib import Path


SOURCES = ('linkedin', 'indeed', 'naukri', 'googlejobs')


def run_jobs(args):
    from utils.linkedin import LinkedIn

    if args.driver != 'chromedriver':
        print('note: --driver is ignored now that LinkedIn runs on Playwright, '
              'which manages its own browser')

    operator = LinkedIn(path = args.driver, headless = args.Display)

    fp = Path(args.cookies)

    if fp.exists() and not fp.is_dir() and not args.overwrite:
        operator.restore_session(fp)
    else:
        user = input('Username/Email ID: ')
        passw = getpass.getpass()
        operator.login(username=user, password=passw, twoFA=args.twofa, filepath=fp, overwrite=args.overwrite)

    operator.scrape_jobs(args.jobs)
    # Uncooment for LinkedIn's easy apply
    # operator.easy_apply(args.jobs)


def run_reviews(args):
    from utils.reviews import AmbitionBoxClient, GlassdoorClient, ReviewsError

    sources = ['ambitionbox', 'glassdoor'] if args.source == 'both' else [args.source]
    results = []

    for source in sources:
        if source == 'ambitionbox':
            client = AmbitionBoxClient()
        else:
            client = GlassdoorClient(profile_dir=args.profile, login=args.login,
                                     headless=args.Display)
        try:
            rating = client.fetch(args.company, max_reviews=args.max_reviews)
        except ReviewsError as error:
            # one blocked source should not throw away the other one's results
            print('{}: {}'.format(source, error))
            continue

        print('{}: {} - {} out of 5 from {} ratings ({} reviews fetched)'.format(
            source, rating.company, rating.overall_rating, rating.review_count,
            len(rating.reviews)))
        results.append(rating.to_dict())

    if not results:
        return 1

    with open(args.output, 'w', encoding='utf-8') as file:
        json.dump(results if len(results) > 1 else results[0], file, indent=2, ensure_ascii=False)
    print('written to {}'.format(args.output))
    return 0


def _filters(args):
    from utils.jobsearch import JobFilter

    return JobFilter(
        title=args.title,
        company=args.company,
        location=args.location,
        min_salary=args.min_salary,
        currency=args.currency,
        posted_within_days=args.posted_within,
        keep_unknown=not args.strict,
    )


def run_search(args):
    from utils.jobs import save_jobs
    from utils.jobsearch import SOURCES as ALL, Jobs

    wanted = ALL if 'all' in args.source else args.source
    board = Jobs(sources=wanted, headless=not args.show)
    found = board.search(args.keywords, _filters(args), limit=args.limit)

    if not found:
        print('nothing matched')
        return 1

    total = save_jobs(found, args.output)
    print('{} jobs written to {} ({} stored in total)'.format(len(found), args.output, total))
    return 0


def run_apply(args):
    from utils.jobs import Job
    from utils.jobsearch import Jobs

    try:
        with open(args.input, 'r', encoding='utf-8') as file:
            stored = json.load(file).get('list', [])
    except (FileNotFoundError, ValueError) as error:
        print('could not read {}: {}'.format(args.input, error))
        return 1

    known = {field for field in Job.__dataclass_fields__}
    jobs = [Job(**{key: value for key, value in item.items() if key in known})
            for item in stored]

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


def _add_filters(command):
    command.add_argument('-t', '--title', help='keep jobs whose title contains these words')
    command.add_argument('-c', '--company', help='keep jobs from companies matching this')
    command.add_argument('--min-salary', type=float, help='annual salary floor, in --currency')
    command.add_argument('--currency', help='currency for --min-salary, e.g. INR or USD')
    command.add_argument('--posted-within', type=int, metavar='DAYS',
                         help='keep jobs posted within this many days')
    command.add_argument('--strict', action='store_true',
                         help='drop jobs whose salary or date could not be read '
                              '(they are kept and flagged by default)')


parser = argparse.ArgumentParser(description='Scrape and apply to jobs, and look up company ratings')
commands = parser.add_subparsers(dest='command')

jobs = commands.add_parser('jobs', help='scrape (and optionally apply to) LinkedIn jobs')
jobs.add_argument('-c', '--cookies', default='cookies.json', help='file path to where cookies are or to store them')
jobs.add_argument("-w", "--overwrite", action="store_true", help='overwrite existing session if it already exists')
jobs.add_argument('-d', '--driver', default='chromedriver', help='path to Chrome browser driver')
jobs.add_argument("-t", "--twofa", action="store_true", help='use it if you have 2 Factor Authentication enabled')
jobs.add_argument('-j', '--jobs', default='job_listing.json', help='file path to where jobs urls are or to store them')
jobs.add_argument("-D", "--Display", action="store_false", help='Whether to display the browser or not (headless mode)')
jobs.set_defaults(handler=run_jobs)

search = commands.add_parser('search', help='search job boards without signing in')
search.add_argument('keywords', help='what to search for, e.g. "python developer"')
search.add_argument('-l', '--location', default='', help='where to search')
search.add_argument('-s', '--source', default=['all'], nargs='+',
                    choices=list(SOURCES) + ['all'], help='which boards to search')
search.add_argument('-n', '--limit', type=int, default=25,
                    help='jobs to pull per board, before filtering')
search.add_argument('-o', '--output', default='job_listing.json', help='file path to store the scraped jobs')
search.add_argument('--show', action='store_true', help='run the browser visibly, to solve a bot check yourself')
_add_filters(search)
search.set_defaults(handler=run_search)

apply = commands.add_parser('apply', help='apply to stored jobs and log them for a spreadsheet')
apply.add_argument('-i', '--input', default='job_listing.json', help='file path to the scraped jobs')
apply.add_argument('--log', default='applied_jobs.csv', help='CSV to append applications to')
apply.add_argument('-l', '--location', default=None, help='only apply to jobs in this location')
apply.add_argument('--dry-run', action='store_true', help='show what would be applied to, without applying')
apply.add_argument('--show', action='store_true', help='run the browser visibly')
_add_filters(apply)
apply.set_defaults(handler=run_apply)

reviews = commands.add_parser('reviews', help='fetch company ratings and pros/cons')
reviews.add_argument('company', help='AmbitionBox slug (e.g. tcs), or a Glassdoor reviews url / slug with employer id (e.g. Google-E9079)')
reviews.add_argument('-s', '--source', default='ambitionbox', choices=['ambitionbox', 'glassdoor', 'both'], help='where to read ratings from')
reviews.add_argument('-n', '--max-reviews', type=int, default=20, help='how many individual reviews to pull')
reviews.add_argument('-o', '--output', default='company_reviews.json', help='file path to store the scraped ratings')
reviews.add_argument('--login', action='store_true', help='open a visible browser so you can clear the Glassdoor bot check and sign in')
reviews.add_argument('--profile', default='.gd_profile', help='directory holding the reused Glassdoor browser profile')
reviews.add_argument("-D", "--Display", action="store_false", help='Whether to display the browser or not (headless mode)')
reviews.set_defaults(handler=run_reviews)

# Driver Code

# `python run.py` and `python run.py -c cookies.json` kept working the way the README
# documents them, so bare flags still mean the LinkedIn job run
argv = sys.argv[1:]
if not argv or argv[0].startswith('-') and argv[0] not in ('-h', '--help'):
    argv = ['jobs'] + argv

args = parser.parse_args(argv)

if not getattr(args, 'handler', None):
    parser.print_help()
    sys.exit(1)

sys.exit(args.handler(args) or 0)
