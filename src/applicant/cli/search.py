"""`applicant search`: the job boards, filtered, merged into the listing file."""

from __future__ import annotations

import argparse
from typing import Any, get_args

from pydantic import Field, PositiveInt, ValidationInfo, model_validator

from ..errors import ConfigError
from ..log import get
from ..services.search import SOURCES
from ..settings import FilterOptions, SavedSearch, Settings, SourceName
from .common import add_filters, configured_path, filters_from, positive_int
from .dispatch import AddParser, Request

logger = get(__name__)


def add_parser(add: AddParser) -> argparse.ArgumentParser:
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
        choices=get_args(SourceName),
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


class Overrides(SavedSearch, Request):
    saved: str | None = None
    list_saved: bool = False
    show: bool = False


class Search(FilterOptions, Request):
    location: str | None = ''
    keywords: str = ''
    saved: str | None = None
    list_saved: bool = False
    show: bool = False
    source: list[SourceName] = Field(default_factory=lambda: ['all'])
    limit: PositiveInt = 25
    want: PositiveInt | None = None
    max_rounds: PositiveInt = 4
    enrich: bool = False
    enrich_limit: PositiveInt = 25
    output: str | None = None

    @model_validator(mode='before')
    @classmethod
    def resolve(cls, raw: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        overrides = Overrides.model_validate(raw)
        values: dict[str, Any] = {}
        if overrides.saved is not None and not overrides.list_saved:
            settings = info.context
            if not isinstance(settings, Settings):
                raise ValueError('saved search requires settings')
            saved = settings.searches.get(overrides.saved)
            if saved is None:
                known = (
                    ', '.join(sorted(settings.searches))
                    or 'none - add [searches.<name>] to applicant.toml'
                )
                raise ConfigError(f'no saved search {overrides.saved!r}; saved: {known}')
            values = saved.model_dump(exclude_none=True)
        values.update(overrides.model_dump(exclude_none=True))
        if not overrides.list_saved and not values.get('keywords'):
            raise ConfigError('search needs keywords, or --saved NAME for a search that has them')
        return values


def list_saved(saved: dict[str, SavedSearch]) -> int:
    if not saved:
        print('no saved searches - add [searches.<name>] tables to applicant.toml')
        return 0
    for name, search in sorted(saved.items()):
        fields = search.model_dump(exclude_none=True)
        if search.keywords is not None:
            fields = {'keywords': search.keywords, **fields}
        print('{}: {}'.format(name, ', '.join(f'{key}={value!r}' for key, value in fields.items())))
    return 0


def run(args: Search, settings: Settings) -> int:
    from ..search import Jobs
    from ..services.search import store

    if args.list_saved:
        return list_saved(settings.searches)
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

    stored = store(
        found, configured_path(settings, args.output, settings.files.listing), settings.store
    )
    print(
        '{} jobs written to {} ({} stored in total)'.format(stored.saved, stored.path, stored.total)
    )
    return 0
