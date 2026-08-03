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


def run_search(args):
    from utils.jobs import JobsError, save_jobs

    wanted = SOURCES if 'all' in args.source else args.source
    headless = not args.show
    found = []

    for name in wanted:
        if name == 'linkedin':
            from utils.linkedin import LinkedIn
            client = LinkedIn(headless=headless)
        elif name == 'indeed':
            from utils.indeed import Indeed
            client = Indeed(headless=headless)
        elif name == 'naukri':
            from utils.naukri import Naukri
            client = Naukri(headless=headless)
        else:
            from utils.googlejobs import GoogleJobs
            client = GoogleJobs(headless=headless)

        try:
            jobs = client.search(args.keywords, args.location, limit=args.limit)
        except JobsError as error:
            # one blocked board should not cost us the others
            print('{}: {}'.format(name, error))
            continue
        finally:
            if hasattr(client, 'close'):
                client.close()

        print('{}: {} jobs'.format(name, len(jobs)))
        found.extend(jobs)

    if not found:
        return 1

    total = save_jobs(found, args.output)
    print('{} jobs written to {} ({} stored in total)'.format(len(found), args.output, total))
    return 0


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
search.add_argument('-n', '--limit', type=int, default=25, help='jobs to pull per board')
search.add_argument('-o', '--output', default='job_listing.json', help='file path to store the scraped jobs')
search.add_argument('--show', action='store_true', help='run the browser visibly, to solve a bot check yourself')
search.set_defaults(handler=run_search)

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
