"""`applicant reviews`: a company's ratings from AmbitionBox and Glassdoor."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from typing import TYPE_CHECKING, Literal

from ..services.reviews import REVIEW_SOURCES
from ..settings import Settings
from .common import configured_path
from .dispatch import AddParser, Request
from .render import Renderer, terminal

if TYPE_CHECKING:
    from ..reviews import AmbitionBoxClient, GlassdoorClient


class ReviewsRequest(Request):
    company: str
    source: Literal['ambitionbox', 'glassdoor', 'both'] = 'ambitionbox'
    max_reviews: int = 20
    output: str | None = None
    login: bool = False
    profile: str | None = None
    show: bool = False


def add_parser(add: AddParser) -> argparse.ArgumentParser:
    reviews = add('reviews', help='fetch company ratings and pros/cons')
    reviews.add_argument(
        'company',
        help='AmbitionBox slug (e.g. tcs), or a Glassdoor reviews url / '
        'slug with employer id (e.g. Google-E9079)',
    )
    reviews.add_argument(
        '-s',
        '--source',
        default='ambitionbox',
        choices=[*REVIEW_SOURCES, 'both'],
        help='where to read ratings from',
    )
    reviews.add_argument(
        '-n', '--max-reviews', type=int, default=20, help='how many individual reviews to pull'
    )
    reviews.add_argument(
        '-o',
        '--output',
        help='file path to store the scraped ratings (default: [files] reviews, '
        'company_reviews.json)',
    )
    reviews.add_argument(
        '--login',
        action='store_true',
        help='open a visible browser so you can clear the Glassdoor bot check and sign in',
    )
    reviews.add_argument(
        '--profile',
        help='directory holding the reused Glassdoor browser profile (default: [files] '
        'glassdoor_profile, .gd_profile)',
    )
    reviews.add_argument('--show', action='store_true', help='run the browser visibly')
    return reviews


def clients(
    args: ReviewsRequest, settings: Settings
) -> dict[str, AmbitionBoxClient | GlassdoorClient]:
    """One place that knows which client a --source names."""
    from ..reviews import AmbitionBoxClient, GlassdoorClient

    wanted = REVIEW_SOURCES if args.source == 'both' else (args.source,)
    made: dict[str, AmbitionBoxClient | GlassdoorClient] = {}
    for source in wanted:
        if source == 'ambitionbox':
            made[source] = AmbitionBoxClient()
        else:
            made[source] = GlassdoorClient(
                profile_dir=configured_path(
                    settings, args.profile, settings.files.glassdoor_profile
                ),
                login=args.login,
                headless=not args.show,
                interaction=terminal(),
            )
    return made


def run(args: ReviewsRequest, settings: Settings) -> int:
    from ..services.reviews import fetch_reviews

    # the clients are made here, so they are closed here - whatever happens
    with ExitStack() as opened:
        made = {
            name: opened.enter_context(client) for name, client in clients(args, settings).items()
        }
        found = fetch_reviews(
            args.company,
            made,
            configured_path(settings, args.output, settings.files.reviews),
            max_reviews=args.max_reviews,
            emit=Renderer(),
            backend=settings.store,
        )
    if found.written_to is None:
        return 1
    print('written to {}'.format(found.written_to))
    return 0
