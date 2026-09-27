"""`applicant jobs`: LinkedIn's recommended jobs, signed in."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..log import get
from .common import add_logging
from .render import terminal

logger = get(__name__)


def add_parser(add) -> argparse.ArgumentParser:
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
    add_logging(jobs)
    return jobs


def run(args) -> int:
    from ..boards.linkedin import LinkedIn

    if args.driver != 'chromedriver':
        logger.warning(
            'note: --driver is ignored now that LinkedIn runs on Playwright, '
            'which manages its own browser'
        )

    settings = args.settings
    fp = Path(settings.path(args.cookies))
    with LinkedIn(path=args.driver, headless=args.Display, interaction=terminal()) as operator:
        if fp.exists() and not fp.is_dir() and not args.overwrite:
            logger.info(f'jobs: restoring the LinkedIn session from {fp}')
            if operator.restore_session(fp):
                print('session restored from {}'.format(fp))
        else:
            # the credentials go to the browser and nowhere else - never to a log
            logger.info(f'jobs: signing in to LinkedIn, session to be saved at {fp}')
            user = terminal().ask('Username/Email ID: ')
            passw = terminal().ask('Password: ', secret=True)
            saved = operator.login(
                username=user,
                password=passw,
                twoFA=args.twofa,
                filepath=fp,
                overwrite=args.overwrite,
            )
            if saved is not None:
                print('session saved to {}'.format(saved))

        listing = settings.path(args.jobs)
        jobs = operator.scrape_jobs(listing)
    print('scraped {} recommended jobs into {}'.format(len(jobs), listing))
    return 0
