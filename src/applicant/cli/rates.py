"""`applicant rates`: the cached PPP factors, and topping them up."""

from __future__ import annotations

import argparse

from .common import add_logging
from .render import Renderer


def add_parser(add) -> argparse.ArgumentParser:
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
    add_logging(rates)
    return rates


def run(args) -> int:
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

        done = refresh(wanted, force=args.force, emit=Renderer())
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
            print('written to ppp_factors.json - commit it so others start with them')

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
