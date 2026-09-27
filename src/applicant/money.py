"""Currency conversion for comparing salaries across countries.

Two different questions, two different numbers:

* **market** - what the pay converts to at today's exchange rate. Right for
  "how much is this worth in my currency".
* **ppp** - purchasing power parity, what the pay is worth *where it is earned*.
  Right for "which of these is the better job". A market conversion makes every
  Indian salary look small next to a US one; PPP is the honest comparison.

Rates come from the ECB via frankfurter.dev, PPP conversion factors from the
World Bank indicator PA.NUS.PPP (local currency per international $). Both are
cached on disk. The World Bank API throttles aggressively, so factors are
fetched one country at a time, only when needed, and kept indefinitely - the
figures are annual.

Nothing here is ever guessed. When a factor cannot be had, the caller is told so
and falls back to a market rate rather than being handed an invented number.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from .domain.rates import (
    BASE,
    COUNTRY_CURRENCY,
    CURRENCY_COUNTRY,
    RateSnapshot,
    RateTable,
)
from .domain.rates import convert as _convert
from .errors import SourceError
from .files import read_document, write_document
from .infra.http import HttpClient

logger = logging.getLogger(__name__)

FX_URL = 'https://api.frankfurter.dev/v1/latest'
WB_URL = 'https://api.worldbank.org/v2/country/{}/indicator/PA.NUS.PPP?format=json'
# mrnev=1 asks for the most recent non-empty value, so one small response per country
WB_PARAMS = {'format': 'json', 'mrnev': '1'}

CACHE_PATH = os.environ.get('APPLICANT_MONEY_CACHE', '.money_cache.json')
# PPP factors refreshed by `applicant rates --refresh`. Beside the user's data,
# never in the package: an installed package is not somewhere a run can - or
# should - write. The shipped table (FACTORS_PATH) is read-only and this one is
# layered over it.
LOCAL_FACTORS: str | Path = 'ppp_factors.json'
# the checked in reference table, so a fresh clone starts with what is known
FACTORS_PATH = Path(__file__).with_name('ppp_factors.json')
FX_TTL = 24 * 3600  # exchange rates move daily
PPP_TTL = 180 * 24 * 3600  # PPP factors are published yearly


def configure(cache: str | Path | None = None, factors: str | Path | None = None) -> None:
    """Where the rate cache and the refreshed PPP table live - set from Settings.

    Resets the shared `rates()` table, so the next lookup uses the new paths.
    """
    global CACHE_PATH, LOCAL_FACTORS, _DEFAULT
    if cache is not None:
        CACHE_PATH = str(cache)
    if factors is not None:
        LOCAL_FACTORS = factors
    _DEFAULT = None


def load_factors(path: str | Path | None = None):
    """PPP factors: country -> {value, year}.

    With no path, the shipped table with the locally refreshed one layered over
    it. With a path, that file alone. Missing or unreadable reads as empty
    rather than raising, so a broken data file degrades to fetching on demand
    instead of stopping a search.
    """
    if path is None:
        return {**_read_factors(FACTORS_PATH), **_read_factors(LOCAL_FACTORS)}
    return _read_factors(path)


def _read_factors(path: str | Path):
    try:
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        logger.warning(f'PPP table {path} unreadable ({type(error).__name__}); fetching on demand')
        return {}
    factors = payload.get('factors') if isinstance(payload, dict) else None
    return factors if isinstance(factors, dict) else {}


# Only values actually retrieved from the World Bank live here - see the note in
# ppp_factors.json. USA is 1.0 by definition: the international dollar is the US
# dollar.
PPP_SEED = load_factors(FACTORS_PATH)


class Rates:
    """Disk cached FX rates and PPP factors."""

    def __init__(self, path=None, timeout=20.0, offline=False):
        self.path = path if path is not None else CACHE_PATH
        self.timeout = timeout
        self.offline = offline
        self._cache = self._load()

    def _load(self):
        # only a cache: an unreadable one is reported and rebuilt, not kept
        cache = read_document(self.path)
        cache.setdefault('fx', {})
        cache.setdefault('ppp', {})
        for country, entry in load_factors().items():
            cache['ppp'].setdefault(country, dict(entry, fetched=0, seeded=True))
        return cache

    def _save(self):
        try:
            write_document(self.path, self._cache)
        except OSError as error:
            # a read only cwd should not break a search, but it should be visible
            logger.warning(f'could not write the rate cache {self.path}: {error}')

    # -- exchange rates ---------------------------------------------------

    def fx(self, base='USD'):
        """{currency: units per 1 base}. Empty dict when unavailable."""
        entry = self._cache['fx'].get(base)
        if entry and time.time() - entry.get('fetched', 0) < FX_TTL:
            return entry['rates']
        if self.offline:
            return entry['rates'] if entry else {}

        try:
            # one attempt, as before: a failure is not cached and the salary
            # filter asks once per job, so retrying here would stall an offline
            # run on every posting. Phase 2 fetches rates once, up front.
            with HttpClient('frankfurter.dev', timeout=self.timeout, retries=1, interval=0) as http:
                payload = http.get(FX_URL, params={'base': base}).json()
            rates = dict(payload['rates'])
            rates[base] = 1.0
        except Exception as error:  # noqa: BLE001 - any FX failure falls back to the cache
            logger.warning(
                f'exchange rates for {base} unavailable ({type(error).__name__}: {error}); '
                + ('using the cached ones' if entry else 'market conversion is off')
            )
            return entry['rates'] if entry else {}
        logger.info(f'fetched {len(rates)} exchange rates against {base}')

        self._cache['fx'][base] = {
            'rates': rates,
            'fetched': time.time(),
            'date': payload.get('date'),
        }
        self._save()
        return rates

    # -- ppp factors ------------------------------------------------------

    def ppp(self, currency):
        """Local currency units per international $, or None if unknown."""
        country = CURRENCY_COUNTRY.get((currency or '').upper())
        if not country:
            return None

        entry = self._cache['ppp'].get(country)
        if entry and (entry.get('seeded') or time.time() - entry.get('fetched', 0) < PPP_TTL):
            return entry['value']
        if self.offline:
            return entry['value'] if entry else None

        newest = fetch_factor(country, timeout=self.timeout)
        if newest is None:
            logger.warning(
                f'no PPP factor for {country}; '
                + ('using the stale one' if entry else 'falling back to market rates')
            )
            # the World Bank throttles hard; a stale or seeded value beats nothing
            return entry['value'] if entry else None

        self._cache['ppp'][country] = dict(newest, fetched=time.time())
        self._save()
        return newest['value']

    # -- a fixed view for one run -----------------------------------------

    def snapshot(self, currencies, basis: str = 'ppp') -> RateSnapshot:
        """The figures needed to compare these currencies, fetched now, once.

        PPP factors are one lookup per currency, and only for ones not already
        cached. Market rates are one request whatever the currencies, and are
        only asked for when they will be used: on the market basis, or as the
        fallback for a currency with no PPP factor - exactly when a comparison
        made job by job would have asked. What cannot be had is left out, and
        `convert` reports it rather than guesses.
        """
        wanted = sorted({code.upper() for code in currencies if code})
        if len(wanted) < 2:
            return RateSnapshot()  # nothing to compare across
        factors = {}
        if basis == 'ppp':
            factors = {code: value for code in wanted if (value := self.ppp(code)) is not None}
        needs_market = basis != 'ppp' or len(factors) < len(wanted)
        market = dict(self.fx(BASE)) if needs_market else {}
        return RateSnapshot(market=market, ppp_factors=factors)


def _series(response: httpx.Response) -> list[dict] | None:
    """The rows of a World Bank answer that carry a value, or None if it is
    not an answer at all - a throttle message, an error page, a reshaped body.

    An empty list is an answer: the series has no data for that country.
    """
    try:
        payload = response.json()
        return [row for row in payload[1] if row.get('value') is not None]
    except Exception:  # noqa: BLE001 - any shape but the expected one is unusable
        return None


def fetch_factor(country, timeout=20.0, attempts=3, pause=2.0, client=None):
    """One country's newest PPP factor -> {value, year}, or None.

    The World Bank answers roughly one country per attempt under load, and
    sometimes throttles with HTTP 200 and an error body, so an answer that is
    not a series is retried like a throttling status rather than read as one.
    `pause` is the backoff base and the gap between requests to the API.
    """
    http = HttpClient(
        'the World Bank',
        client=client,
        timeout=timeout,
        retries=attempts,
        backoff=pause,
        interval=pause,
        retry_when=lambda response: _series(response) is None,
    )
    try:
        with http:
            response = http.get(WB_URL.format(country), params=WB_PARAMS)
    except SourceError as error:  # unreachable, or throttled through every attempt
        logger.debug(f'PPP factor for {country}: {type(error).__name__}: {error}')
        return None

    rows = _series(response)
    if not rows:
        # None: the retries ran out on unusable answers. []: a real answer,
        # the series has no data for this country
        return None
    rows.sort(key=lambda row: row['date'], reverse=True)
    return {'value': rows[0]['value'], 'year': rows[0]['date']}


def refresh_factors(
    path: str | Path | None = None,
    currencies=None,
    force=False,
    pause=1.0,
    timeout=20.0,
    on_result=None,
    client=None,
):
    """Fetch PPP factors into the local table (or into `path`).

    -> (updated, failed, skipped), each a list of country codes.

    By default the factors go to LOCAL_FACTORS, and a country the shipped table
    already has counts as known. Pass the shipped table's own path to update it
    - which is how a maintainer tops up what a fresh clone starts with.

    Only what actually comes back is written. USA stays at 1.0 by definition and
    is never fetched. Countries already recorded are skipped unless `force`.
    """
    wanted = (
        CURRENCY_COUNTRY.values()
        if currencies is None
        else [
            CURRENCY_COUNTRY[code.upper()]
            for code in currencies
            if code.upper() in CURRENCY_COUNTRY
        ]
    )

    # quarantined, not overwritten: this is the checked in table, and writing only
    # this run's factors over an unreadable copy would silently drop the rest
    target = LOCAL_FACTORS if path is None else path
    document = read_document(target, quarantine=True)
    factors = document.setdefault('factors', {})
    if not isinstance(factors, dict):
        factors = document['factors'] = {}
    # what counts as already known: this file, and the shipped table under it
    known = {**PPP_SEED, **factors} if path is None else factors

    updated, failed, skipped = [], [], []
    today = time.strftime('%Y-%m-%d')

    for country in dict.fromkeys(wanted):
        if country == 'USA' or (country in known and not force):
            skipped.append(country)
            if on_result:
                on_result(country, known.get(country), True)
            continue

        found = fetch_factor(country, timeout=timeout, pause=pause, client=client)
        if found is None:
            failed.append(country)
        else:
            factors[country] = dict(found, retrieved=today)
            updated.append(country)
        if on_result:
            on_result(country, factors.get(country), False)

    if updated:
        document['factors'] = dict(sorted(factors.items()))
        write_document(target, document, trailing_newline=True)
        logger.info(f'PPP table {target}: {len(updated)} factor(s) written')

    return updated, failed, skipped


_DEFAULT = None


def rates():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Rates()
    return _DEFAULT


def convert(amount, source, target, basis='ppp', table: RateTable | None = None):
    """-> (converted, note), against the shared live cache unless given a table.

    The arithmetic is `applicant.domain.rates.convert`; this only supplies the
    default table, which may fetch on a miss. Pass a `RateSnapshot` to keep a
    conversion off the network.
    """
    return _convert(amount, source, target, basis=basis, table=table or rates())


__all__ = [
    'BASE',
    'COUNTRY_CURRENCY',
    'CURRENCY_COUNTRY',
    'FACTORS_PATH',
    'RateSnapshot',
    'Rates',
    'convert',
    'fetch_factor',
    'load_factors',
    'rates',
    'refresh_factors',
]
