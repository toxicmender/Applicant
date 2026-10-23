"""Converting pay between currencies, from figures already in hand.

Nothing here fetches anything. `convert` takes a `RateTable` - anything with
`fx()` and `ppp()` - and a `RateSnapshot` is the plain-data one: the exchange
rates and PPP factors a run needs, fetched once up front by
`applicant.money.Rates.snapshot()`. Filtering against a snapshot cannot block
on the network, and every job in a run is compared at the same rate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from . import flags

# Which economy a currency belongs to, for the PPP lookup. ISO 4217 -> ISO 3166
# alpha-3, which is what the World Bank keys on.
#
# Two caveats worth knowing before reading a converted figure:
#
# * EUR maps to the euro area aggregate (EMU). Price levels differ a lot between
#   Ireland and Portugal, so a euro figure is coarser than a single country one.
# * A currency used by several economies (USD, EUR) is priced at the economy
#   named here, not wherever the job actually is.
#
# TWD is deliberately absent: Taiwan is not a World Bank member, so there is no
# PA.NUS.PPP series to fetch and a PPP comparison would have to be invented.
CURRENCY_COUNTRY = {
    # requested explicitly
    'USD': 'USA',
    'GBP': 'GBR',
    'INR': 'IND',
    'NZD': 'NZL',
    'AUD': 'AUS',
    'EUR': 'EMU',
    'SEK': 'SWE',
    # the rest of the majors, by trade volume
    'JPY': 'JPN',
    'CHF': 'CHE',
    'CAD': 'CAN',
    'CNY': 'CHN',
    'HKD': 'HKG',
    'SGD': 'SGP',
    'NOK': 'NOR',
    'DKK': 'DNK',
    'KRW': 'KOR',
    'PLN': 'POL',
    'CZK': 'CZE',
    'HUF': 'HUN',
    'RON': 'ROU',
    'TRY': 'TUR',
    'ILS': 'ISR',
    'ZAR': 'ZAF',
    'MXN': 'MEX',
    'BRL': 'BRA',
    'CLP': 'CHL',
    'COP': 'COL',
    'ARS': 'ARG',
    'AED': 'ARE',
    'SAR': 'SAU',
    'EGP': 'EGY',
    'NGN': 'NGA',
    'KES': 'KEN',
    'PKR': 'PAK',
    'BDT': 'BGD',
    'LKR': 'LKA',
    'NPR': 'NPL',
    'IDR': 'IDN',
    'MYR': 'MYS',
    'THB': 'THA',
    'PHP': 'PHL',
    'VND': 'VNM',
}

# the reverse, for reporting - first currency wins where several share a country
COUNTRY_CURRENCY: dict[str, str] = {}
for _currency, _country in CURRENCY_COUNTRY.items():
    COUNTRY_CURRENCY.setdefault(_country, _currency)

# market rates are quoted against this, and converted through it
BASE = 'USD'


class RateTable(Protocol):
    """Where conversion figures come from: a snapshot, or the live cache."""

    def fx(self, base: str = BASE) -> Mapping[str, float]:
        """{currency: units per 1 base}. Empty when unavailable."""
        ...

    def ppp(self, currency: str) -> float | None:
        """Local currency units per international $, or None if unknown."""
        ...


@dataclass(frozen=True)
class RateSnapshot:
    """The figures one run compares pay with, fixed for the length of the run.

    `market` is units of each currency per 1 USD; `ppp` is each currency's
    units per international dollar. A currency missing from either is simply
    unknown, which `convert` reports rather than guesses around.
    """

    market: Mapping[str, float] = field(default_factory=dict)
    ppp_factors: Mapping[str, float] = field(default_factory=dict)

    def fx(self, base: str = BASE) -> Mapping[str, float]:
        base = base.upper()
        if base == BASE:
            return self.market
        pivot = self.market.get(base)
        if not pivot:
            return {}
        return {currency: rate / pivot for currency, rate in self.market.items()}

    def ppp(self, currency: str) -> float | None:
        return self.ppp_factors.get((currency or '').upper())


# no figures at all: every cross-currency comparison is reported as unconverted
EMPTY = RateSnapshot()


def convert(
    amount: float | None,
    source: str | None,
    target: str | None,
    basis: str = 'ppp',
    table: RateTable = EMPTY,
) -> tuple[float | None, str | None]:
    """-> (converted, note). `note` is None when the conversion was exact.

    basis 'ppp' compares purchasing power, 'market' uses the exchange rate.
    A missing PPP factor degrades to a market rate and says so in the note
    rather than inventing anything.
    """
    if amount is None or not source or not target:
        return None, flags.CURRENCY_UNKNOWN

    source, target = source.upper(), target.upper()
    if source == target:
        return amount, None

    if basis == 'ppp':
        source_ppp, target_ppp = table.ppp(source), table.ppp(target)
        if source_ppp and target_ppp:
            # local -> international $ -> the other local currency
            return amount / source_ppp * target_ppp, None
        converted, note = _market(amount, source, target, table)
        return converted, note or flags.PPP_UNAVAILABLE

    return _market(amount, source, target, table)


def _market(
    amount: float, source: str, target: str, table: RateTable
) -> tuple[float | None, str | None]:
    rate_table = table.fx(BASE)
    source_rate, target_rate = rate_table.get(source), rate_table.get(target)
    if not source_rate or not target_rate:
        return None, flags.RATE_UNAVAILABLE
    return amount / source_rate * target_rate, None
