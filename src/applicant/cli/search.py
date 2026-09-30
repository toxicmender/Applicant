"""`applicant search`: the job boards, filtered, merged into the listing file."""

from __future__ import annotations

import argparse
import sys

from ..log import get
from ..services.search import SOURCES
from .common import add_filters, file_arg, filters_from, positive_int

logger = get(__name__)


def add_parser(add) -> argparse.ArgumentParser:
    search = add('search', help='search job boards without signing in')
    search.add_argument(
        'keywords',
        nargs='?',
        help='what to search for, e.g. "python developer" (optional with --saved)',
    )
    search.add_argument(
        '--saved',
        metavar='NAME',
        help='run a search saved in applicant.toml under [searches.NAME]; flags '
        'given here override what it saved',
    )
    search.add_argument(
        '--list-saved', action='store_true', help='list the saved searches and exit'
    )
    search.add_argument('-l', '--location', help='where to search')
    search.add_argument(
        '-s',
        '--source',
        nargs='+',
        choices=[*SOURCES, 'all'],
        help='which boards to search',
    )
    search.add_argument(
        '-n',
        '--limit',
        type=positive_int,
        help='jobs to pull per board, before filtering (default 25)',
    )
    search.add_argument(
        '--want',
        type=positive_int,
        metavar='N',
        help='keep reading each board until this many jobs survive the filter, '
        'rather than filtering a fixed pull of --limit',
    )
    search.add_argument(
        '--max-rounds',
        type=positive_int,
        metavar='N',
        help='how many times --want may re-read a board, each round doubling the '
        'pull and re-reading what it already saw (default 4)',
    )
    search.add_argument(
        '-o',
        '--output',
        help='file path to store the scraped jobs (default: [files] listing, job_listing.json)',
    )
    search.add_argument(
        '--show', action='store_true', help='run the browser visibly, to solve a bot check yourself'
    )
    search.add_argument(
        '--enrich',
        action='store_true',
        default=None,
        help='read the postings themselves to answer an --experience filter their '
        'search cards could not. Slow, and only where a board offers a readable '
        'posting page - currently LinkedIn',
    )
    search.add_argument(
        '--enrich-limit',
        type=positive_int,
        metavar='N',
        help='how many postings --enrich may read in one run (default 25)',
    )
    add_filters(search)
    return search


# what an unset search flag means, once any saved search has had its say
DEFAULTS = {
    'location': '',
    'source': ['all'],
    'limit': 25,
    'max_rounds': 4,
    'enrich': False,
    'enrich_limit': 25,
}


class UsageError(Exception):
    """A search that cannot run as asked: exit 2, like argparse's own errors."""


def resolve(args, saved: dict) -> argparse.Namespace:
    """The search to run: the command line, then the saved search, then defaults.

    A flag left unset is None, so "not given" can be told from "given"; only
    those are filled from `--saved`, and only then from DEFAULTS.
    """
    values = vars(args).copy()
    if args.saved is not None:
        if args.saved not in saved:
            known = ', '.join(sorted(saved)) or 'none - add [searches.<name>] to applicant.toml'
            raise UsageError(f'no saved search {args.saved!r}; saved: {known}')
        for field, value in saved[args.saved].model_dump(exclude_none=True).items():
            if values.get(field) is None:
                values[field] = value
    for field, value in DEFAULTS.items():
        if values.get(field) is None:
            values[field] = value
    if not values.get('keywords'):
        raise UsageError('search needs keywords, or --saved NAME for a search that has them')
    return argparse.Namespace(**values)


def list_saved(saved: dict) -> int:
    if not saved:
        print('no saved searches - add [searches.<name>] tables to applicant.toml')
        return 0
    for name, search in sorted(saved.items()):
        fields = search.model_dump(exclude_none=True)
        print('{}: {}'.format(name, ', '.join(f'{key}={value!r}' for key, value in fields.items())))
    return 0


def run(args) -> int:
    from ..search import Jobs
    from ..services.search import store

    settings = args.settings
    if args.list_saved:
        return list_saved(settings.searches)
    try:
        args = resolve(args, settings.searches)
    except UsageError as error:
        print('applicant search: {}'.format(error), file=sys.stderr)
        return 2

    wanted = SOURCES if 'all' in args.source else args.source
    logger.info(
        f'search: {args.keywords!r} on {", ".join(wanted)}, up to {args.limit} per board'
        + (f' (saved search {args.saved!r})' if args.saved else '')
    )
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

    stored = store(found, file_arg(args, 'output', 'listing'), settings.store)
    print(
        '{} jobs written to {} ({} stored in total)'.format(stored.saved, stored.path, stored.total)
    )
    return 0
