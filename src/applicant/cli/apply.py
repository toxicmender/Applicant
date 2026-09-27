"""`applicant apply`: the stored jobs that match, applied to or listed."""

from __future__ import annotations

import argparse

from ..log import get
from .common import add_filters, add_logging, filters_from

logger = get(__name__)


def add_parser(add) -> argparse.ArgumentParser:
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
    add_filters(apply_)
    add_logging(apply_)
    return apply_


def run(args) -> int:
    from ..search import Jobs
    from ..services.apply import worklist

    work = worklist(args.input, filters_from(args))
    if not work.read:
        print('no jobs to apply to in {}'.format(args.input))
        return 1

    print('{} of {} stored jobs match'.format(len(work.matching), work.read))
    if not work.matching:
        return 1

    logger.info(
        f'apply: {len(work.matching)} job(s) to {args.log}' + (' (dry run)' if args.dry_run else '')
    )
    with Jobs(headless=not args.show) as board:
        board.apply(work.matching, log=args.log, dry_run=args.dry_run)
    return 0
