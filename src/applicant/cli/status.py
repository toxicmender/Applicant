"""`applicant status`: what is stored right now."""

from __future__ import annotations

import argparse

from ..files import write_document
from ..log import get
from .common import file_arg

logger = get(__name__)


def add_parser(add) -> argparse.ArgumentParser:
    status = add('status', help='summarise stored jobs and applications')
    status.add_argument(
        '-i',
        '--input',
        help='file path to the scraped jobs (default: [files] listing, job_listing.json)',
    )
    status.add_argument(
        '--log',
        help='CSV the applications were appended to (default: [files] applications, '
        'applied_jobs.csv)',
    )
    status.add_argument('--json', help='also write the summary to this file as JSON')
    return status


def run(args) -> int:
    """What is stored right now: jobs found, and what came of them."""
    from ..services.status import summarise

    settings = args.settings
    status = summarise(
        file_arg(args, 'input', 'listing'), file_arg(args, 'log', 'applications'), settings.store
    )

    print('{} jobs stored in {}'.format(status.jobs, status.input))
    for source in sorted(status.by_source):
        print('  {}: {}'.format(source, status.by_source[source]))

    print('{} applications recorded in {}'.format(status.applications, status.log))
    for name in sorted(status.by_status):
        print('  {}: {}'.format(name, status.by_status[name]))

    if args.json:
        target = settings.path(args.json)
        # all at once or not at all, like every other file the CLI writes
        write_document(target, status.to_dict())
        logger.info(f'status: summary written to {target}')
        print('written to {}'.format(target))
    return 0
