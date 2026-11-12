"""`applicant financials`: company funding, tracked across runs."""

from __future__ import annotations

import argparse
import os
import shlex
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from pydantic import Field

from ..services.financials import FINANCIAL_SOURCES
from ..settings import Settings
from .common import configured_path
from .dispatch import AddParser, Request
from .render import Renderer, shown, terminal

if TYPE_CHECKING:
    from ..financials import CrunchbaseClient, TracxnClient
    from ..services.financials import FinancialsSource


class FinancialsRequest(Request):
    companies: list[str] = Field(default_factory=list)
    from_jobs: str | None = None
    source: Literal['crunchbase', 'tracxn', 'both'] = 'both'
    max_rounds: int = 20
    output: str | None = None
    rounds: bool = False
    login: bool = False
    show: bool = False
    cb_profile: str | None = None
    tx_profile: str | None = None


def add_parser(add: AddParser) -> argparse.ArgumentParser:
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
        help='file that keeps the history and is compared against (default: [files] '
        'financials, company_financials.json)',
    )
    financials.add_argument('--rounds', action='store_true', help='print each funding round too')
    financials.add_argument(
        '--crunchbase-key',
        help='Crunchbase API key (default: $CRUNCHBASE_API_KEY); without one the website is read',
    )
    financials.add_argument(
        '--tracxn-key',
        help='Tracxn API token (default: $TRACXN_API_KEY); otherwise use a profile URL or interactive lookup',
    )
    financials.add_argument(
        '--login',
        action='store_true',
        help='open a visible browser so you can clear the bot check and sign in',
    )
    financials.add_argument('--show', action='store_true', help='run the browser visibly')
    financials.add_argument(
        '--cb-profile',
        help='directory holding the reused Crunchbase browser profile (default: [files] '
        'crunchbase_profile, .cb_profile)',
    )
    financials.add_argument(
        '--tx-profile',
        help='directory holding the reused Tracxn browser profile (default: [files] '
        'tracxn_profile, .tx_profile)',
    )
    return financials


def clients(
    args: FinancialsRequest, settings: Settings
) -> dict[str, CrunchbaseClient | TracxnClient]:
    """One place that knows which client a --source names."""
    from ..financials import CrunchbaseClient, TracxnClient

    wanted = FINANCIAL_SOURCES if args.source == 'both' else (args.source,)
    made: dict[str, CrunchbaseClient | TracxnClient] = {}
    if 'crunchbase' in wanted:
        made['crunchbase'] = CrunchbaseClient(
            # flag, then $CRUNCHBASE_API_KEY, resolved by Settings
            api_key=settings.secret('crunchbase_key'),
            profile_dir=configured_path(
                settings, args.cb_profile, settings.files.crunchbase_profile
            ),
            login=args.login,
            headless=not args.show,
            interaction=terminal(),
        )
    if 'tracxn' in wanted:
        made['tracxn'] = TracxnClient(
            api_key=settings.secret('tracxn_key'),
            profile_dir=configured_path(settings, args.tx_profile, settings.files.tracxn_profile),
            login=args.login,
            headless=not args.show,
            interaction=terminal(),
        )
    return made


def run(args: FinancialsRequest, settings: Settings) -> int:
    from ..services.financials import companies_to_track, track_financials

    if args.login:
        from .setup import legacy_login

        for name in FINANCIAL_SOURCES if args.source == 'both' else (args.source,):
            code = legacy_login(
                settings, name, profile=args.cb_profile if name == 'crunchbase' else args.tx_profile
            )
            if code:
                return code
        args = args.model_copy(update={'login': False})

    companies = companies_to_track(
        args.companies,
        settings.path(args.from_jobs) if args.from_jobs else None,
        settings.store,
    )
    if not companies:
        print('name at least one company, or pass --from-jobs job_listing.json')
        return 1

    # the clients are made here, so they are closed here - whatever happens
    with ExitStack() as opened:
        opened_clients = {
            name: opened.enter_context(client) for name, client in clients(args, settings).items()
        }
        made: dict[str, FinancialsSource] = dict(opened_clients)
        if (tracxn := opened_clients.get('tracxn')) is not None:
            from .tracxn_lookup import InteractiveTracxn

            profile = configured_path(settings, args.tx_profile, settings.files.tracxn_profile)
            reauth = 'uv run applicant setup provider tracxn --reauth'
            if Path(profile) != Path('.tx_profile'):
                absolute = shown(Path(profile).resolve())
                quoted = (
                    "'" + absolute.replace("'", "''") + "'"
                    if os.name == 'nt'
                    else shlex.quote(absolute)
                )
                reauth = f'uv run applicant financials -s tracxn --login --tx-profile {quoted}'
            resolver = InteractiveTracxn(cast('TracxnClient', tracxn), terminal(), reauth)
            opened.callback(resolver.close)
            made['tracxn'] = resolver
        tracked = track_financials(
            companies,
            made,
            configured_path(settings, args.output, settings.files.financials),
            max_rounds=args.max_rounds,
            emit=Renderer(rounds=args.rounds, failures=True),
            backend=settings.store,
        )
    if not tracked.found:
        return 1
    print('tracked in {}'.format(tracked.output))
    return 0
