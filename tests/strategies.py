"""Hypothesis strategies for the text the boards hand us, and the run profiles.

Scraped pay, dates, places and experience come from strangers, so the parsers
are fuzzed with the pieces that text is really made of - every digit grouping
the boards use, currency signs and codes, units, pay periods in the languages
the boards write in, dashes and "to" - mixed with arbitrary unicode.

Profiles, chosen by HYPOTHESIS_PROFILE:
- `ci` (the default): 150 examples per property, derandomized, so every run -
  and every Python version in CI - tries the same inputs and cannot flake.
- `deep`: 20,000 examples with a random seed, for a soak on a workstation.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st

settings.register_profile(
    'ci',
    max_examples=150,
    derandomize=True,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile('deep', max_examples=20_000, deadline=None)
settings.load_profile(os.environ.get('HYPOTHESIS_PROFILE', 'ci'))

SEPARATORS = [',', '.', "'", '’', ' ', '\u00a0', '\u202f', '']
CURRENCIES = [
    '$',
    'US$',
    'CA$',
    'A$',
    'S$',
    'R$',
    '₹',
    'Rs.',
    '€',
    '£',
    '¥',
    'zł',
    'USD',
    'INR',
    'EUR',
    'GBP',
    'CHF',
    'JPY',
    'try',
    'cop',
]
UNITS = ['k', 'K', 'L', 'Lacs', 'lakhs', 'LPA', 'Cr', 'crore', 'Mio', 'mn', 'M', 'Tsd', 'tausend']
PERIODS = [
    'a year',
    'per annum',
    'PA',
    'p.a.',
    'a month',
    'pro Monat',
    'par an',
    'an hour',
    '/hr',
    'a day',
    'per week',
    '年収',
    '月給',
]
JOINERS = ['-', '–', '—', '−', '~', ' to ', ' bis ', ' à ', ' hasta ', ' do ']
WORDS = [
    'Salary',
    'up to',
    'From',
    'Negotiable',
    'Not disclosed',
    'Unpaid',
    'plus',
    'bonus',
    '+ equity',
    '(',
    ')',
    ':',
]
ODD = [
    '١٢٣',
    '１２３',
    '²',
    '1e9',
    '9' * 30,
    '\u200f',
    '\u2028',
    '\x1b[2J',
    '\r',
    '\t',
    '🙂',
    '  ',
    '',
]


@st.composite
def figures(draw) -> str:
    """'45,000', '12,00,000', "110'000", '2.5', '9999999...'."""
    head = draw(st.integers(0, 999))
    groups = draw(st.lists(st.integers(0, 999), max_size=4))
    sep = draw(st.sampled_from(SEPARATORS))
    text = str(head) + ''.join(
        sep + str(group).zfill(draw(st.sampled_from([2, 3]))) for group in groups
    )
    if draw(st.booleans()):
        text += draw(st.sampled_from(['.', ','])) + str(draw(st.integers(0, 99)))
    return text


def pieces() -> st.SearchStrategy[str]:
    return st.one_of(
        figures(),
        st.sampled_from(CURRENCIES),
        st.sampled_from(UNITS),
        st.sampled_from(PERIODS),
        st.sampled_from(JOINERS),
        st.sampled_from(WORDS),
        st.sampled_from(ODD),
        st.text(max_size=4),
    )


def scraped() -> st.SearchStrategy[str]:
    """Text shaped like what boards print, with arbitrary unicode mixed in."""
    joined = st.lists(pieces(), max_size=8).flatmap(
        lambda parts: st.sampled_from([' ', '']).map(lambda glue: glue.join(parts))
    )
    return st.one_of(joined, st.text(max_size=40))


def mutated(examples: list[str]) -> st.SearchStrategy[str]:
    """Known-good strings from the table tests, with a piece put in, taken out or swapped."""

    @st.composite
    def one(draw) -> str:
        text = draw(st.sampled_from(examples))
        at = draw(st.integers(0, len(text)))
        width = draw(st.integers(0, 3))
        return text[:at] + draw(pieces()) + text[at + width :]

    return one()


def json_like() -> st.SearchStrategy[object]:
    """Payload shapes a source's API or embedded state might hand back."""
    scalars = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-(10**15), 10**15),
        st.floats(allow_nan=True, allow_infinity=True),
        scraped(),
    )
    keys = st.sampled_from(
        [
            'value',
            'amount',
            'currency',
            'value_usd',
            'amountInUSD',
            'text',
            'formatted',
            'USD',
            'INR',
            'totalAmount',
            'unit',
            'x',
        ]
    )
    return st.recursive(
        scalars,
        lambda inner: st.one_of(
            st.lists(inner, max_size=3), st.dictionaries(keys, inner, max_size=4)
        ),
        max_leaves=10,
    )
