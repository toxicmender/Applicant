"""`applicant rates`: the cached PPP factors, and topping them up."""

from __future__ import annotations

import argparse

from pydantic import model_validator

from ..settings import Settings
from .dispatch import AddParser, Request
from .render import Renderer


class Rates(Request):
    refresh: bool = False
    force: bool = False
    currency: list[str] | None = None
    into: str | None = None

    @model_validator(mode='after')
    def refresh_only(self):
        if not self.refresh and (self.force or self.currency is not None or self.into is not None):
            raise ValueError('--force, --currency and --into require --refresh')
        return self


def add_parser(add: AddParser) -> argparse.ArgumentParser:
    rates = add('rates', help='show or refresh the locally cached PPP conversion factors')
    rates.add_argument(
        '--refresh',
        action='store_true',
        help='fetch missing factors from the World Bank and record them',
    )
    rates.add_argument(
        '--force', action='store_true', help='re-fetch factors that are already recorded'
    )
    rates.add_argument(
        '-c',
        '--currency',
        nargs='+',
        metavar='CODE',
        help='limit the refresh to these currencies, e.g. GBP SEK NZD',
    )
    rates.add_argument(
        '--into',
        metavar='PATH',
        help='write refreshed factors here instead of the local table - e.g. '
        'src/applicant/ppp_factors.json, to update what a fresh clone starts with',
    )
    return rates


def run(args: Rates, settings: Settings) -> int:
    """Show, or top up, the locally cached PPP factors."""
    from ..services.rates import cached, refresh

    if args.refresh:
        wanted = args.currency or None
        print(
            'fetching PPP factors from the World Bank{}...'.format(
                '' if wanted is None else ' for ' + ', '.join(c.upper() for c in wanted)
            )
        )
        print('one country at a time - the API throttles hard, so this is not quick.\n')

        into = settings.path(args.into) if args.into else None
        done = refresh(wanted, force=args.force, emit=Renderer(), into=into)
        if done.unknown:
            print('not a mapped currency: {}'.format(', '.join(done.unknown)))
            if wanted and len(done.unknown) == len(wanted):
                return 2
        print(
            '\n{} updated, {} failed, {} already known'.format(
                len(done.updated), len(done.failed), len(done.skipped)
            )
        )
        if done.failed:
            print(
                'not recorded (nothing is written from memory): ' + ', '.join(sorted(done.failed))
            )
        if done.updated:
            print('written to {}'.format(done.path))
        elif done.failed:
            # asked for factors and got none: a script must not read that as done
            return 1

    table = cached()
    print(
        '\n{} of {} mapped currencies have a local PPP factor'.format(table.known, table.countries)
    )
    for row in table.rows:
        if row.value is None:
            print('  {} ({}): not cached - fetched on demand'.format(row.currency, row.country))
        else:
            print(
                '  {} ({}): {} per international $ ({})'.format(
                    row.currency, row.country, row.value, row.year
                )
            )
    return 0
