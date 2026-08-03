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

import httpx

FX_URL = 'https://api.frankfurter.dev/v1/latest'
WB_URL = 'https://api.worldbank.org/v2/country/{}/indicator/PA.NUS.PPP?format=json'

CACHE_PATH = os.environ.get('APPLICANT_MONEY_CACHE', '.money_cache.json')
FX_TTL = 24 * 3600          # exchange rates move daily
PPP_TTL = 180 * 24 * 3600   # PPP factors are published yearly

# which economy a currency belongs to, for the PPP lookup
CURRENCY_COUNTRY = {
    'INR': 'IND', 'USD': 'USA', 'GBP': 'GBR', 'EUR': 'EMU', 'CAD': 'CAN',
    'AUD': 'AUS', 'SGD': 'SGP', 'AED': 'ARE', 'CHF': 'CHE', 'JPY': 'JPN',
    'BRL': 'BRA', 'MXN': 'MEX', 'PLN': 'POL', 'SEK': 'SWE', 'ZAR': 'ZAF',
    'NZD': 'NZL',
}

# Seeded from the World Bank on 2026-08-03; only values actually retrieved are
# listed. Everything else is fetched on demand and cached. USA is 1.0 by
# definition - the international dollar is the US dollar.
PPP_SEED = {
    'USA': {'value': 1.0, 'year': 'definition'},
    'IND': {'value': 20.088629, 'year': '2025'},
    'JPN': {'value': 97.08005, 'year': '2025'},
}


class Rates:
    """Disk cached FX rates and PPP factors."""

    def __init__(self, path=CACHE_PATH, timeout=20.0, offline=False):
        self.path = path
        self.timeout = timeout
        self.offline = offline
        self._cache = self._load()

    def _load(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as handle:
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
        except Exception:
            return entry['rates'] if entry else {}

        self._cache['fx'][base] = {'rates': rates, 'fetched': time.time(),
                                   'date': payload.get('date')}
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

        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
                payload = client.get(WB_URL.format(country)).json()
            rows = [row for row in payload[1] if row.get('value') is not None]
            rows.sort(key=lambda row: row['date'], reverse=True)
            newest = rows[0]
        except Exception:
            # the World Bank throttles hard; a stale or seeded value beats nothing
            return entry['value'] if entry else None

        self._cache['ppp'][country] = {'value': newest['value'], 'year': newest['date'],
                                       'fetched': time.time()}
        self._save()
        return newest['value']


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
