"""`applicant financials`: company funding, tracked across runs."""

from __future__ import annotations

import argparse

from ..services.financials import FINANCIAL_SOURCES
from .render import Renderer, terminal


def add_parser(add) -> argparse.ArgumentParser:
    financials = add(
        'financials',
        help='track company funding, valuation and revenue from Crunchbase and Tracxn',
    )
    financials.add_argument(
        'companies',
        nargs='*',
        help='company names, Crunchbase permalinks/urls, or Tracxn domains/ids/profile urls',
    )
    financials.add_argument(
        '--from-jobs',
        metavar='PATH',
        help='also track every company in a scraped jobs file, e.g. job_listing.json',
    )
    financials.add_argument(
        '-s',
        '--source',
        default='both',
        choices=[*FINANCIAL_SOURCES, 'both'],
        help='where to read financials from',
    )
    financials.add_argument(
        '-n', '--max-rounds', type=int, default=20, help='funding rounds to pull per company'
    )
    financials.add_argument(
        '-o',
        '--output',
        default='company_financials.json',
        help='file that keeps the history and is compared against',
    )
    financials.add_argument('--rounds', action='store_true', help='print each funding round too')
    financials.add_argument(
        '--crunchbase-key',
        help='Crunchbase API key (default: $CRUNCHBASE_API_KEY); without one the website is read',
    )
    financials.add_argument(
        '--tracxn-key',
        help='Tracxn API token (default: $TRACXN_API_KEY); without one a profile url is needed',
    )
    financials.add_argument(
        '--login',
        action='store_true',
        help='open a visible browser so you can clear the bot check and sign in',
    )
    financials.add_argument('--show', action='store_true', help='run the browser visibly')
    financials.add_argument(
        '--cb-profile',
        default='.cb_profile',
        help='directory holding the reused Crunchbase browser profile',
    )
    financials.add_argument(
        '--tx-profile',
        default='.tx_profile',
        help='directory holding the reused Tracxn browser profile',
    )
    return financials


def clients(args) -> dict:
    """One place that knows which client a --source names."""
    from ..financials import CrunchbaseClient, TracxnClient

    wanted = FINANCIAL_SOURCES if args.source == 'both' else (args.source,)
    made = {}
    if 'crunchbase' in wanted:
        made['crunchbase'] = CrunchbaseClient(
            # flag, then $CRUNCHBASE_API_KEY, resolved by Settings
            api_key=args.settings.secret('crunchbase_key'),
            profile_dir=args.settings.path(args.cb_profile),
            login=args.login,
            headless=not args.show,
            interaction=terminal(),
        )
    if 'tracxn' in wanted:
        made['tracxn'] = TracxnClient(
            api_key=args.settings.secret('tracxn_key'),
            profile_dir=args.settings.path(args.tx_profile),
            login=args.login,
            headless=not args.show,
            interaction=terminal(),
        )
    return made


def run(args) -> int:
    from ..services.financials import companies_to_track, track_financials

    settings = args.settings
    companies = companies_to_track(
        args.companies,
        settings.path(args.from_jobs) if args.from_jobs else None,
        settings.store,
    )
    if not companies:
        print('name at least one company, or pass --from-jobs job_listing.json')
        return 1

    tracked = track_financials(
        companies,
        clients(args),
        settings.path(args.output),
        max_rounds=args.max_rounds,
        emit=Renderer(rounds=args.rounds, failures=True),
        backend=settings.store,
    )
    if not tracked.found:
        return 1
    print('tracked in {}'.format(tracked.output))
    return 0
