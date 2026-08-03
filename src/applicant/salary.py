"""Reading pay off a job posting.

Boards write pay a dozen different ways and most postings state none at all, so
`parse_salary` returns None for "unknown" and callers must never read that as zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CURRENCIES = [
    (re.compile(r'₹|\bINR\b|\bRs\.?\b', re.IGNORECASE), 'INR'),
    (re.compile(r'\$|\bUSD\b', re.IGNORECASE), 'USD'),
    (re.compile(r'€|\bEUR\b', re.IGNORECASE), 'EUR'),
    (re.compile(r'£|\bGBP\b', re.IGNORECASE), 'GBP'),
]

# how many of each pay period make a year
PERIODS = [
    (re.compile(r'\b(an?\s+)?hour|\bhourly\b|\b/\s*hr\b|\bper hour\b', re.IGNORECASE), 2080),
    (re.compile(r'\b(a\s+)?day\b|\bdaily\b|\bper day\b', re.IGNORECASE), 260),
    (re.compile(r'\b(a\s+)?week\b|\bweekly\b|\bper week\b', re.IGNORECASE), 52),
    (re.compile(r'\b(a\s+)?month\b|\bmonthly\b|\bPM\b|\bp\.?m\.?\b', re.IGNORECASE), 12),
    (
        re.compile(
            r'\b(a\s+)?year\b|\byearly\b|\bannum\b|\bannual\b|\bPA\b|\bp\.?a\.?\b', re.IGNORECASE
        ),
        1,
    ),
]

MULTIPLIERS = [
    (re.compile(r'^(cr|crore)', re.IGNORECASE), 10_000_000),
    (re.compile(r'^(l|lac|lacs|lakh|lakhs)', re.IGNORECASE), 100_000),
    (re.compile(r'^k\b', re.IGNORECASE), 1_000),
    (re.compile(r'^m\b', re.IGNORECASE), 1_000_000),
]

AMOUNT = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]*)')

UNPRICED = re.compile(r'not disclosed|unpaid|negotiable', re.IGNORECASE)

# the rupee units that imply their own currency
RUPEE_SCALES = (100_000, 10_000_000)


@dataclass
class Salary:
    low: float | None = None
    high: float | None = None
    currency: str | None = None
    period_per_year: int = 1

    @property
    def annual_high(self) -> float | None:
        return self.high * self.period_per_year if self.high is not None else None

    @property
    def annual_low(self) -> float | None:
        return self.low * self.period_per_year if self.low is not None else None


def parse_salary(text: str | None) -> Salary | None:
    """'2-2.5 Lacs PA' -> Salary(200000, 250000, 'INR', 1). None when unparseable."""
    if not text:
        return None
    cleaned = text.replace('–', '-').replace('—', '-').replace('−', '-')
    if UNPRICED.search(cleaned):
        return None

    currency = None
    for pattern, code in CURRENCIES:
        if pattern.search(cleaned):
            currency = code
            break

    period = 1
    for pattern, factor in PERIODS:
        if pattern.search(cleaned):
            period = factor
            break

    amounts = []
    scale = 1
    for raw, suffix in AMOUNT.findall(cleaned):
        try:
            value = float(raw.replace(',', ''))
        except ValueError:
            continue
        multiplier = 1
        for pattern, factor in MULTIPLIERS:
            if suffix and pattern.match(suffix):
                multiplier = factor
                break
        scale = max(scale, multiplier)
        amounts.append((value, multiplier))

    if not amounts:
        return None

    # ranges carry the unit only on the last number ("2-2.5 Lacs"), so a bare
    # value in the same string takes the unit that was stated
    values = [value * (multiplier if multiplier > 1 else scale) for value, multiplier in amounts]

    # 'Lacs' and 'Cr' are themselves rupee units
    if currency is None and scale in RUPEE_SCALES:
        currency = 'INR'

    # a lone bare number that looks like a year is not pay
    if (
        len(values) == 1
        and currency is None
        and scale == 1
        and float(values[0]).is_integer()
        and 1900 <= values[0] <= 2100
    ):
        return None

    return Salary(low=min(values), high=max(values), currency=currency, period_per_year=period)
