"""`applicant reviews`: a company's ratings from AmbitionBox and Glassdoor."""

from __future__ import annotations

import argparse

from ..services.reviews import REVIEW_SOURCES
from .render import Renderer, terminal


def add_parser(add) -> argparse.ArgumentParser:
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
        default='company_reviews.json',
        help='file path to store the scraped ratings',
    )
    reviews.add_argument(
        '--login',
        action='store_true',
        help='open a visible browser so you can clear the Glassdoor bot check and sign in',
    )
    reviews.add_argument(
        '--profile',
        default='.gd_profile',
        help='directory holding the reused Glassdoor browser profile',
    )
    reviews.add_argument('--show', action='store_true', help='run the browser visibly')
    return reviews


def clients(args) -> dict:
    """One place that knows which client a --source names."""
    from ..reviews import AmbitionBoxClient, GlassdoorClient

    wanted = REVIEW_SOURCES if args.source == 'both' else (args.source,)
    made = {}
    for source in wanted:
        if source == 'ambitionbox':
            made[source] = AmbitionBoxClient()
        else:
            made[source] = GlassdoorClient(
                profile_dir=args.settings.path(args.profile),
                login=args.login,
                headless=not args.show,
                interaction=terminal(),
            )
    return made


def run(args) -> int:
    from ..services.reviews import fetch_reviews

    settings = args.settings
    found = fetch_reviews(
        args.company,
        clients(args),
        settings.path(args.output),
        max_reviews=args.max_reviews,
        emit=Renderer(),
        backend=settings.store,
    )
    if found.written_to is None:
        return 1
    print('written to {}'.format(found.written_to))
    return 0
