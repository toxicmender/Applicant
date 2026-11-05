"""`applicant apply`: the stored jobs that match, applied to or listed."""

from __future__ import annotations

import argparse
import sys

from ..domain.job import Job
from ..log import get
from ..settings import FilterOptions, Settings
from .common import add_filters, configured_path, filters_from
from .dispatch import AddParser, Request
from .render import shown, terminal

logger = get(__name__)


class Apply(FilterOptions, Request):
    input: str | None = None
    log: str | None = None
    cookies: str | None = None
    dry_run: bool = False
    show: bool = False
    yes: bool = False


def add_parser(add: AddParser) -> argparse.ArgumentParser:
    apply_ = add('apply', help='apply to stored jobs and log them for a spreadsheet')
    apply_.add_argument(
        '-i',
        '--input',
        help='file path to the scraped jobs (default: [files] listing, job_listing.json)',
    )
    apply_.add_argument(
        '--log',
        help='CSV to append applications to (default: [files] applications, applied_jobs.csv)',
    )
    apply_.add_argument(
        '-l', '--location', default=None, help='only apply to jobs in this location'
    )
    apply_.add_argument(
        '--dry-run', action='store_true', help='show what would be applied to, without applying'
    )
    apply_.add_argument('--show', action='store_true', help='run the browser visibly')
    apply_.add_argument(
        '--cookies',
        help='the signed-in session `applicant jobs` saved (default: [files] cookies, '
        'cookies.json)',
    )
    add_filters(apply_)
    apply_.add_argument(
        '-y',
        '--yes',
        action='store_true',
        help='submit without asking first. Without it, the applications about to '
        'be sent are listed and you are asked to confirm; with no terminal to ask '
        'on, nothing is submitted',
    )
    return apply_


def run(args: Apply, settings: Settings) -> int:
    from ..search import Jobs
    from ..services.apply import worklist

    source, log = (
        configured_path(settings, args.input, settings.files.listing),
        configured_path(settings, args.log, settings.files.applications),
    )
    work = worklist(source, filters_from(args), settings.store)
    if not work.read:
        print('no jobs to apply to in {}'.format(source))
        return 1

    print('{} of {} stored jobs match'.format(len(work.matching), work.read))
    if not work.matching:
        return 1

    logger.info(
        f'apply: {len(work.matching)} job(s) to {log}' + (' (dry run)' if args.dry_run else '')
    )
    declined: list[Job] = []

    def confirm(targets: list[Job]) -> bool:
        if args.yes:
            return True
        print('about to submit {} application(s) in your name:'.format(len(targets)))
        for job in targets:
            # scraped text, escaped: nothing in a posting may rewrite this list
            print(
                '  {} - {} ({})'.format(shown(job.title), shown(job.company or '?'), shown(job.url))
            )
        if not sys.stdin.isatty():
            print('no terminal to confirm on: nothing submitted. Pass --yes to submit anyway.')
            declined.extend(targets)
            return False
        answered = terminal().ask('type yes to submit them: ').strip().lower() == 'yes'
        if not answered:
            declined.extend(targets)
        return answered

    session = configured_path(settings, args.cookies, settings.files.cookies)
    with Jobs(headless=not args.show, backend=settings.store, session=session) as board:
        board.apply(work.matching, log=log, dry_run=args.dry_run, confirm=confirm)
    if declined:
        print('not submitted: {} application(s); nothing was logged for them'.format(len(declined)))
    return 0
