"""`applicant search`: the job boards, filtered, merged into the listing file."""

from __future__ import annotations

import argparse

from ..log import get
from ..services.search import SOURCES
from .common import add_filters, filters_from

logger = get(__name__)


def add_parser(add) -> argparse.ArgumentParser:
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
    search.add_argument(
        '--enrich',
        action='store_true',
        help='read the postings themselves to answer an --experience filter their '
        'search cards could not. Slow, and only where a board offers a readable '
        'posting page - currently LinkedIn',
    )
    search.add_argument(
        '--enrich-limit',
        type=int,
        default=25,
        metavar='N',
        help='how many postings --enrich may read in one run (default 25)',
    )
    add_filters(search)
    return search


def run(args) -> int:
    from ..search import Jobs
    from ..services.search import store

    settings = args.settings
    wanted = SOURCES if 'all' in args.source else args.source
    logger.info(f'search: {args.keywords!r} on {", ".join(wanted)}, up to {args.limit} per board')
    with Jobs(sources=wanted, headless=not args.show, backend=settings.store) as board:
        found = board.search(
            args.keywords,
            filters_from(args),
            limit=args.limit,
            want=args.want,
            max_rounds=args.max_rounds,
            enrich=args.enrich,
            enrich_limit=args.enrich_limit,
        )

    if not found:
        logger.info('search: no job survived the filters')
        print('nothing matched')
        return 1

    stored = store(found, settings.path(args.output), settings.store)
    print(
        '{} jobs written to {} ({} stored in total)'.format(stored.saved, stored.path, stored.total)
    )
    return 0
