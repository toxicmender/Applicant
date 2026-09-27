"""`applicant status`: what is stored right now."""

from __future__ import annotations

import argparse
import json

from ..log import get
from .common import add_logging

logger = get(__name__)


def add_parser(add) -> argparse.ArgumentParser:
    status = add('status', help='summarise stored jobs and applications')
    status.add_argument(
        '-i', '--input', default='job_listing.json', help='file path to the scraped jobs'
    )
    status.add_argument(
        '--log', default='applied_jobs.csv', help='CSV the applications were appended to'
    )
    status.add_argument('--json', help='also write the summary to this file as JSON')
    add_logging(status)
    return status


def run(args) -> int:
    """What is stored right now: jobs found, and what came of them."""
    from ..services.status import summarise

    status = summarise(args.input, args.log)

    print('{} jobs stored in {}'.format(status.jobs, args.input))
    for source in sorted(status.by_source):
        print('  {}: {}'.format(source, status.by_source[source]))

    print('{} applications recorded in {}'.format(status.applications, args.log))
    for name in sorted(status.by_status):
        print('  {}: {}'.format(name, status.by_status[name]))

    if args.json:
        with open(args.json, 'w', encoding='utf-8') as file:
            json.dump(status.to_dict(), file, indent=2)
        logger.info(f'status: summary written to {args.json}')
        print('written to {}'.format(args.json))
    return 0
