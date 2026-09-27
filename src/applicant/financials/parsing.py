"""Helpers shared by the Crunchbase and Tracxn clients.

Neither source publishes a stable schema for its web pages, and Tracxn does not
publish response fields for its API either, so payloads are read by walking them
and matching on field names rather than indexing fixed paths - the same approach
the Glassdoor client takes with Apollo caches.
"""

from __future__ import annotations

import json
import re

from .models import Money

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
LD_JSON = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
# Crunchbase is Angular; its transfer state has been called both of these
NG_STATE = re.compile(r'<script id="(?:ng-state|client-app-state)"[^>]*>(.*?)</script>', re.DOTALL)

# longest first, so 'US$' wins over '$'
SYMBOLS = [
    ('US$', 'USD'),
    ('S$', 'SGD'),
    ('A$', 'AUD'),
    ('C$', 'CAD'),
    ('HK$', 'HKD'),
    ('$', 'USD'),
    ('₹', 'INR'),
    ('€', 'EUR'),
    ('£', 'GBP'),
    ('¥', 'JPY'),
    ('Rs.', 'INR'),
    ('Rs', 'INR'),
]
CODES = {
    'USD',
    'INR',
    'EUR',
    'GBP',
    'JPY',
    'CNY',
    'SGD',
    'AUD',
    'CAD',
    'HKD',
    'CHF',
    'AED',
    'SEK',
    'BRL',
    'IDR',
    'KRW',
    'ILS',
    'NZD',
    'ZAR',
    'MXN',
}

UNITS = [
    (re.compile(r'^(t|tn|trillion)$', re.I), 1e12),
    (re.compile(r'^(b|bn|billion)$', re.I), 1e9),
    (re.compile(r'^(m|mn|mm|mil|million)$', re.I), 1e6),
    (re.compile(r'^(k|th|thousand)$', re.I), 1e3),
    (re.compile(r'^(cr|crs|crore|crores)$', re.I), 1e7),
    (re.compile(r'^(l|lac|lacs|lakh|lakhs)$', re.I), 1e5),
]
INDIAN_UNITS = (1e7, 1e5)

MONEY = re.compile(
    r'(?P<pre>US\$|HK\$|[SAC]\$|\$|₹|€|£|¥|Rs\.?|\b[A-Z]{3}\b)?\s*'
    r'(?P<num>\d[\d,]*(?:\.\d+)?)\s*'
    r'(?P<unit>trillion|billion|million|thousand|crores?|crs?|lakhs?|lacs?|tn|bn|mn|mm|mil|th|[tbmkl])?\b\.?\s*'
    r'(?P<post>\b[A-Z]{3}\b)?',
    re.IGNORECASE,
)


def parse_money(text):
    """'$2.1B' -> Money(2.1e9, 'USD'). '₹1,200 Cr' -> Money(1.2e10, 'INR'). None if absent.

    'Undisclosed' and friends are None, not zero - an undisclosed round raised
    something, we just do not know what.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if re.search(r'undisclosed|not disclosed|unknown|\bn/a\b|^\s*-+\s*$', text, re.I):
        return None

    for match in MONEY.finditer(text):
        pre, post = match.group('pre'), match.group('post')
        code = _currency(pre) or _currency(post)
        unit = match.group('unit')
        if pre and not _currency(pre) and not pre.startswith(('$', '₹', '€', '£', '¥')):
            code = None  # a random three letter word before a number, e.g. 'ARR 5M'
        try:
            amount = float(match.group('num').replace(',', ''))
        except ValueError:
            continue

        scale = 1
        for pattern, factor in UNITS:
            if unit and pattern.match(unit.rstrip('.')):
                scale = factor
                break
        if code is None and scale in INDIAN_UNITS:
            code = 'INR'  # crore and lakh are themselves rupee units
        # a bare number with no currency and no unit is a count or a year, not money
        if code is None and scale == 1:
            continue

        amount *= scale
        return Money(
            amount=amount,
            currency=code,
            amount_usd=amount if code == 'USD' else None,
            text=text.strip(),
        )
    return None


def _currency(token):
    if not token:
        return None
    token = token.strip()
    for symbol, code in SYMBOLS:
        if token == symbol or token.rstrip('.') == symbol.rstrip('.'):
            return code
    return token.upper() if token.upper() in CODES else None


AMOUNT_KEYS = ('value', 'amount', 'amountInUSD', 'amountUSD', 'amountInUsd', 'value_usd')
USD_KEYS = ('value_usd', 'amountInUSD', 'amountUSD', 'amountInUsd', 'usdAmount', 'valueInUSD')
CURRENCY_KEYS = ('currency', 'currencyCode', 'currency_code', 'unit')


def to_money(value, currency=None):
    """Money out of whatever shape a source used: a number, a string, or a dict.

    Handles Crunchbase's {value, currency, value_usd} and nested envelopes like
    {totalAmount: {amount, currency}} without knowing the envelope's name.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        currency = (currency or 'USD').upper()
        return Money(
            amount=float(value),
            currency=currency,
            amount_usd=float(value) if currency == 'USD' else None,
        )
    if isinstance(value, str):
        return parse_money(value)
    if isinstance(value, list):
        for item in value:
            money = to_money(item, currency)
            if money is not None:
                return money
        return None
    if not isinstance(value, dict):
        return None

    code = next((value[key] for key in CURRENCY_KEYS if isinstance(value.get(key), str)), None)
    usd = next((value[key] for key in USD_KEYS if _number(value.get(key))), None)
    amount = next((value[key] for key in AMOUNT_KEYS if _number(value.get(key))), None)
    if amount is not None:
        if code is None and usd is not None and amount == usd:
            code = 'USD'
        code = code or currency or ('USD' if usd is not None else None)
        code = code.upper() if code else None
        return Money(
            amount=float(amount),
            currency=code,
            amount_usd=float(usd)
            if usd is not None
            else (float(amount) if code == 'USD' else None),
        )
    for key in ('text', 'formatted', 'display', 'label'):
        if isinstance(value.get(key), str):
            money = parse_money(value[key])
            if money is not None:
                return money
    # one level of envelope, e.g. {'totalAmount': {...}} or {'USD': {...}}
    for key, inner in value.items():
        if isinstance(inner, dict):
            money = to_money(inner, key if key.upper() in CODES else currency)
            if money is not None:
                return money
    return None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def walk(node):
    """Yield every dict in a JSON tree, depth first."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            yield current
            stack.extend(reversed(list(current.values())))
        elif isinstance(current, list):
            stack.extend(reversed(current))


def first(node, *keys):
    """The first present, non-empty value among keys."""
    for key in keys:
        value = node.get(key)
        if value not in (None, '', [], {}):
            return value
    return None


def text_of(value):
    """Crunchbase wraps many scalars as {'value': ...}; unwrap them."""
    if isinstance(value, dict):
        value = first(value, 'value', 'name', 'label', 'text')
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(int(value)) if float(value).is_integer() else str(value)
    return value if isinstance(value, str) and value.strip() else None


def embedded_json(html):
    """Every JSON blob a server-rendered page ships: Next.js, Angular state and JSON-LD."""
    found = []
    for pattern in (NEXT_DATA, NG_STATE, LD_JSON):
        for match in pattern.finditer(html or ''):
            raw = match.group(1).strip()
            # Angular escapes the state; undo the common ones before parsing
            raw = (
                raw.replace('&q;', '"')
                .replace('&a;', '&')
                .replace('&s;', "'")
                .replace('&l;', '<')
                .replace('&g;', '>')
            )
            try:
                found.append(json.loads(raw))
            except ValueError:
                continue
    return found


def labelled(text, *labels):
    """The value printed next to a label on a rendered page, e.g. 'Total Funding\\n$2.1B'."""
    for label in labels:
        match = re.search(
            r'(?:^|\n)\s*' + re.escape(label) + r'\s*[:\n]\s*([^\n]+)', text or '', re.I
        )
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


LEGAL_SUFFIX = re.compile(
    r'[\s,.]+(pvt\.?|private|ltd\.?|limited|inc\.?|incorporated|llc|llp|plc|corp\.?|'
    r'corporation|co\.?|company|gmbh|s\.?a\.?|ag|bv|n\.?v\.?)\.?$',
    re.IGNORECASE,
)


def clean_name(name):
    """'Zomato Pvt. Ltd.' -> 'Zomato'. Job boards carry the legal name, data sources the brand."""
    name = (name or '').strip()
    while True:
        stripped = LEGAL_SUFFIX.sub('', name).strip(' ,.')
        if stripped == name or not stripped:
            return name
        name = stripped


def humanize(value):
    """'series_a' -> 'Series A', 'post_ipo_equity' -> 'Post IPO Equity'."""
    if not isinstance(value, str) or not value:
        return value
    if '_' not in value and value != value.lower():
        return value  # already human
    words = []
    for word in value.replace('_', ' ').split():
        words.append(
            word.upper()
            if word.lower() in ('ipo', 'ico', 'spac', 'pe', 'vc')
            else word.upper()
            if len(word) == 1
            else word.capitalize()
        )
    return ' '.join(words)
