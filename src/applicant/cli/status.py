"""`applicant status`: what is stored right now."""

from __future__ import annotations

import argparse

from pydantic import Field

from ..files import write_document
from ..log import get
from ..settings import Settings
from .dispatch import AddParser, Request

logger = get(__name__)


class StatusRequest(Request):
    input: str | None = None
    log: str | None = None
    json_path: str | None = Field(default=None, alias='json')


def add_parser(add: AddParser) -> argparse.ArgumentParser:
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


def run(args: StatusRequest, settings: Settings) -> int:
    """What is stored right now: jobs found, and what came of them."""
    from ..services.status import summarise

    status = summarise(
        settings.path(settings.files.listing if args.input is None else args.input),
        settings.path(settings.files.applications if args.log is None else args.log),
        settings.store,
    )

    print('{} jobs stored in {}'.format(status.jobs, status.input))
    for source in sorted(status.by_source):
        print('  {}: {}'.format(source, status.by_source[source]))

    print('{} applications recorded in {}'.format(status.applications, status.log))
    for name in sorted(status.by_status):
        print('  {}: {}'.format(name, status.by_status[name]))

    if args.json_path:
        target = settings.path(args.json_path)
        # all at once or not at all, like every other file the CLI writes
        write_document(target, status.to_dict())
        logger.info(f'status: summary written to {target}')
        print('written to {}'.format(target))
    return 0
