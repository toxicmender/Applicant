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
import os
import time
from pathlib import Path

import httpx

FX_URL = 'https://api.frankfurter.dev/v1/latest'
WB_URL = 'https://api.worldbank.org/v2/country/{}/indicator/PA.NUS.PPP?format=json'
# mrnev=1 asks for the most recent non-empty value, so one small response per country
WB_PARAMS = {'format': 'json', 'mrnev': '1'}

CACHE_PATH = os.environ.get('APPLICANT_MONEY_CACHE', '.money_cache.json')
# the checked in reference table, so a fresh clone starts with what is known
FACTORS_PATH = Path(__file__).with_name('ppp_factors.json')
FX_TTL = 24 * 3600  # exchange rates move daily
PPP_TTL = 180 * 24 * 3600  # PPP factors are published yearly

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
COUNTRY_CURRENCY = {}
for _currency, _country in CURRENCY_COUNTRY.items():
    COUNTRY_CURRENCY.setdefault(_country, _currency)


def load_factors(path: str | Path = FACTORS_PATH):
    """The checked in PPP table: country -> {value, year}.

    Missing or unreadable reads as empty rather than raising, so a broken data
    file degrades to fetching on demand instead of stopping a search.
    """
    try:
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return {}
    factors = payload.get('factors')
    return factors if isinstance(factors, dict) else {}


# Only values actually retrieved from the World Bank live here - see the note in
# ppp_factors.json. USA is 1.0 by definition: the international dollar is the US
# dollar.
PPP_SEED = load_factors()


class Rates:
    """Disk cached FX rates and PPP factors."""

    def __init__(self, path=CACHE_PATH, timeout=20.0, offline=False):
        self.path = path
        self.timeout = timeout
        self.offline = offline
        self._cache = self._load()

    def _load(self):
        try:
            with open(self.path, encoding='utf-8') as handle:
                cache = json.load(handle)
        except (FileNotFoundError, ValueError):
            cache = {}
        cache.setdefault('fx', {})
        cache.setdefault('ppp', {})
        for country, entry in PPP_SEED.items():
            cache['ppp'].setdefault(country, dict(entry, fetched=0, seeded=True))
        return cache

    def _save(self):
        try:
            with open(self.path, 'w', encoding='utf-8') as handle:
                json.dump(self._cache, handle, indent=1)
        except OSError:
            pass  # a read only cwd should not break a search

    # -- exchange rates ---------------------------------------------------

    def fx(self, base='USD'):
        """{currency: units per 1 base}. Empty dict when unavailable."""
        entry = self._cache['fx'].get(base)
        if entry and time.time() - entry.get('fetched', 0) < FX_TTL:
            return entry['rates']
        if self.offline:
            return entry['rates'] if entry else {}

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                payload = client.get(FX_URL, params={'base': base}).json()
            rates = dict(payload['rates'])
            rates[base] = 1.0
        except Exception:  # noqa: BLE001 - any FX failure falls back to the cache
            return entry['rates'] if entry else {}

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
            # the World Bank throttles hard; a stale or seeded value beats nothing
            return entry['value'] if entry else None

        self._cache['ppp'][country] = dict(newest, fetched=time.time())
        self._save()
        return newest['value']


def fetch_factor(country, timeout=20.0, attempts=3, pause=2.0, client=None):
    """One country's newest PPP factor -> {value, year}, or None.

    The World Bank answers roughly one country per attempt under load, so this
    retries with a widening pause rather than treating a throttle as an answer.
    """
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(pause * attempt)
        try:
            if client is not None:
                response = client.get(WB_URL.format(country), params=WB_PARAMS)
            else:
                with httpx.Client(timeout=timeout, follow_redirects=True) as owned:
                    response = owned.get(WB_URL.format(country), params=WB_PARAMS)
            payload = response.json()
            rows = [row for row in payload[1] if row.get('value') is not None]
        except Exception:  # noqa: BLE001 - throttled, reshaped, or offline
            continue
        if not rows:
            return None  # a real answer: the series has no data for this country
        rows.sort(key=lambda row: row['date'], reverse=True)
        return {'value': rows[0]['value'], 'year': rows[0]['date']}
    return None


def refresh_factors(
    path: str | Path = FACTORS_PATH,
    currencies=None,
    force=False,
    pause=1.0,
    timeout=20.0,
    on_result=None,
    client=None,
):
    """Fetch PPP factors into the checked in reference file.

    -> (updated, failed, skipped), each a list of country codes.

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

    try:
        with open(path, encoding='utf-8') as handle:
            document = json.load(handle)
    except (OSError, ValueError):
        document = {}
    factors = document.setdefault('factors', {})

    updated, failed, skipped = [], [], []
    today = time.strftime('%Y-%m-%d')

    for country in dict.fromkeys(wanted):
        if country == 'USA' or (country in factors and not force):
            skipped.append(country)
            if on_result:
                on_result(country, factors.get(country), True)
            continue

        found = fetch_factor(country, timeout=timeout, pause=pause, client=client)
        if found is None:
            failed.append(country)
        else:
            factors[country] = dict(found, retrieved=today)
            updated.append(country)
        if on_result:
            on_result(country, factors.get(country), False)
        time.sleep(pause)

    if updated:
        document['factors'] = dict(sorted(factors.items()))
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(document, handle, indent=2)
            handle.write('\n')

    return updated, failed, skipped


_DEFAULT = None


def rates():
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Rates()
    return _DEFAULT


def convert(amount, source, target, basis='ppp', table=None):
    """-> (converted, note). `note` is None when the conversion was exact.

    basis 'ppp' compares purchasing power, 'market' uses the exchange rate.
    A missing PPP factor degrades to a market rate and says so in the note
    rather than inventing anything.
    """
    if amount is None or not source or not target:
        return None, 'currency-unknown'

    source, target = source.upper(), target.upper()
    if source == target:
        return amount, None

    table = table or rates()

    if basis == 'ppp':
        source_ppp, target_ppp = table.ppp(source), table.ppp(target)
        if source_ppp and target_ppp:
            # local -> international $ -> the other local currency
            return amount / source_ppp * target_ppp, None
        converted, note = _market(amount, source, target, table)
        return converted, note or 'ppp-unavailable'

    return _market(amount, source, target, table)


def _market(amount, source, target, table):
    rate_table = table.fx('USD')
    source_rate, target_rate = rate_table.get(source), rate_table.get(target)
    if not source_rate or not target_rate:
        return None, 'rate-unavailable'
    return amount / source_rate * target_rate, None
