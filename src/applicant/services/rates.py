"""The PPP factors behind cross-currency pay comparisons: what is cached, and topping it up."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from ..money import CURRENCY_COUNTRY, load_factors, refresh_factors
from .events import Emit, FactorFetched, ignore

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Refreshed:
    updated: list[str]
    failed: list[str]
    skipped: list[str]
    path: str  # where any new factors were written


def refresh(
    currencies: Iterable[str] | None = None,
    force: bool = False,
    emit: Emit = ignore,
    into: str | None = None,
) -> Refreshed:
    """Fetch missing factors (all of them, or these currencies') into the local
    table, or `into` another one - the shipped table, for a maintainer."""

    def report(country, found, was_skipped):
        emit(FactorFetched(country, found, was_skipped))

    updated, failed, skipped = refresh_factors(
        path=into,
        currencies=list(currencies) if currencies else None,
        force=force,
        on_result=report,
    )
    logger.info(
        f'rates: {len(updated)} updated, {len(failed)} failed, {len(skipped)} already known'
    )
    if failed:
        logger.warning(f'rates: no PPP factor returned for {", ".join(sorted(failed))}')
    from .. import money

    return Refreshed(updated, failed, skipped, str(into or money.LOCAL_FACTORS))


@dataclass(frozen=True)
class Factor:
    currency: str
    country: str
    value: float | None  # None: not cached, fetched on demand
    year: str | None


@dataclass(frozen=True)
class FactorTable:
    rows: list[Factor]
    known: int  # countries with a factor
    countries: int  # countries mapped at all


def cached() -> FactorTable:
    """Every mapped currency and the factor held for it, if any."""
    factors = load_factors()
    rows = []
    for currency, country in sorted(CURRENCY_COUNTRY.items()):
        entry = factors.get(country)
        rows.append(
            Factor(
                currency,
                country,
                None if entry is None else entry['value'],
                None if entry is None else str(entry.get('year', '?')),
            )
        )
    countries = set(CURRENCY_COUNTRY.values())
    return FactorTable(rows, sum(1 for country in countries if country in factors), len(countries))
