"""Reading pay off a job posting.

Boards write pay a dozen different ways and most postings state none at all, so
`parse_salary` returns None for "unknown" and callers must never read that as zero.

The ways it is written depend on where the board is. Indeed alone answers from
fifteen country sites, so a figure may be `£45,000`, `45.000 €`, `45 000 €`,
`CHF 110'000`, `¥5,000,000` or `CA$80,000` - and `pro Monat` is a month as much
as `a month` is. A salary is read as its first figure, or its first range: the
rest of the text ("plus 401k", "+ bonus 2L") is not pay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ISO codes, matched as written (upper case) so 'try' or 'cop' in a sentence is
# not a currency. Every currency a pay comparison can convert.
CODES = (
    'USD',
    'INR',
    'EUR',
    'GBP',
    'JPY',
    'CNY',
    'CAD',
    'AUD',
    'NZD',
    'SGD',
    'HKD',
    'CHF',
    'SEK',
    'NOK',
    'DKK',
    'PLN',
    'CZK',
    'HUF',
    'RON',
    'TRY',
    'ILS',
    'ZAR',
    'MXN',
    'BRL',
    'CLP',
    'COP',
    'ARS',
    'AED',
    'SAR',
    'EGP',
    'NGN',
    'KES',
    'PKR',
    'BDT',
    'LKR',
    'NPR',
    'IDR',
    'MYR',
    'THB',
    'PHP',
    'VND',
    'KRW',
)

# The order matters: an explicit code, then a dollar that names its country,
# then a symbol, and a bare '$' only when nothing more specific was said.
CURRENCIES = [
    (re.compile(r'\b({})\b'.format('|'.join(CODES))), None),  # the code itself
    (re.compile(r'(?<![A-Za-z])US\$'), 'USD'),
    (re.compile(r'(?<![A-Za-z])(?:CA|C)\$'), 'CAD'),
    (re.compile(r'(?<![A-Za-z])(?:AU|A)\$'), 'AUD'),
    (re.compile(r'(?<![A-Za-z])NZ\$'), 'NZD'),
    (re.compile(r'(?<![A-Za-z])S\$'), 'SGD'),
    (re.compile(r'(?<![A-Za-z])HK\$'), 'HKD'),
    (re.compile(r'(?<![A-Za-z])R\$'), 'BRL'),
    (re.compile(r'(?<![A-Za-z])MX\$'), 'MXN'),
    (re.compile(r'₹|\bRs\.?(?![A-Za-z])', re.IGNORECASE), 'INR'),
    (re.compile(r'€'), 'EUR'),
    (re.compile(r'£'), 'GBP'),
    (re.compile(r'[¥￥円]'), 'JPY'),
    (re.compile(r'zł', re.IGNORECASE), 'PLN'),
    (re.compile(r'\$'), 'USD'),
]

# how many of each pay period make a year, in the languages the boards write
# in - checked hourly first, so "an hour" is never read as the French "an"
PERIODS = [
    (
        re.compile(
            r'\b(an?\s+)?hour|\bhourly\b|/\s*hr?\b|\bper hour\b|\bstunde|\bstd\b|'
            r'\bheure|\bhora\b|\bora\b|\buur\b|\bgodz|\btimme|時給',
            re.IGNORECASE,
        ),
        2080,
    ),
    (
        re.compile(
            r'\b(a\s+)?day\b|\bdaily\b|\bper day\b|\bpro tag\b|\bpar jour\b|'
            r'\bpor d[ií]a\b|\bal giorno\b|\bper dag\b|日給',
            re.IGNORECASE,
        ),
        260,
    ),
    (
        re.compile(
            r'\b(a\s+)?week\b|\bweekly\b|\bper week\b|\bwoche|\bsemaine|\bsemana|'
            r'\bsettimana|\bvecka|週給',
            re.IGNORECASE,
        ),
        52,
    ),
    (
        re.compile(
            r'\b(a\s+)?month\b|\bmonthly\b|\bPM\b|\bp\.?m\.?\b|/\s*mo\b|\bmonat|\bmois\b|'
            r'\bmensuel|\bmes\b|\bmensual|\bmese\b|\bmensile|\bmaand|\bmiesi[ąę]c|'
            r'\bm[aå]nad|\bmês\b|\bmensal|月給|月収',
            re.IGNORECASE,
        ),
        12,
    ),
    (
        re.compile(
            r'\b(a\s+)?year\b|\byearly\b|\bannum\b|\bannual\b|\bPA\b|\bp\.?a\.?(?!\w)|'
            r'/\s*yr\b|\bjahr|\bjährlich|\bpar an\b|/\s*an\b|\bannuel|\baño\b|\banual\b|'
            r'\banno\b|\bannuo|\bjaar|\brok\b|\brocznie|\bår\b|\bano\b|年収|年俸',
            re.IGNORECASE,
        ),
        1,
    ),
]

MULTIPLIERS = [
    (re.compile(r'^(cr|crore)', re.IGNORECASE), 10_000_000),
    (re.compile(r'^(l|lac|lacs|lakh|lakhs)', re.IGNORECASE), 100_000),
    (re.compile(r'^(k|tsd|tausend)\b', re.IGNORECASE), 1_000),
    (re.compile(r'^(m|mio|mn)\b', re.IGNORECASE), 1_000_000),
]

# A figure, with the grouping any of these boards use: 45,000 / 45.000 /
# 45 000 (also with a no-break or narrow no-break space) / 110'000 - and the
# Indian 12,00,000 - then the unit word, if any, that follows it.
_GROUPED = r"\d{1,3}(?:(?:[.,'’]|[ \u00a0\u202f](?=\d{3}\b))\d{2,3})+(?:[.,]\d{1,2}(?!\d))?"
AMOUNT = re.compile(r'(' + _GROUPED + r'|\d+(?:[.,]\d+)?)\s*([A-Za-z]*)')

# what may stand between the two figures of a range: a dash or a word for "to",
# with a currency sign or unit on either side of it
RANGE = re.compile(
    r'^[^\d\w]{0,3}\s*(?:[A-Za-z]{0,3}\s*)?(?:-|–|—|~|to|bis|à|au|a|hasta|till|tot|do)\s*'
    r'[^\d\w]{0,3}\s*(?:[A-Za-z]{0,3}\$?\s*)?$',
    re.IGNORECASE,
)

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


def number(raw: str, has_unit: bool = False) -> float | None:
    """'45,000' / '45.000' / '45 000' / "110'000" / '12,00,000' -> the value;
    '2.5' and '3,5' stay decimals. `has_unit`: a figure followed by 'Lacs' or
    'k' is short ('2.500 Lacs' is two and a half), never a grouped one."""
    text = raw.replace('\u00a0', ' ').replace('\u202f', ' ').replace('’', "'")
    text = text.replace(' ', '').replace("'", '')
    dots, commas = text.count('.'), text.count(',')
    if dots and commas:
        # both: whichever comes last is the decimal point
        decimal = '.' if text.rfind('.') > text.rfind(',') else ','
        thousands = ',' if decimal == '.' else '.'
        text = text.replace(thousands, '').replace(decimal, '.')
    elif commas:
        whole, _, tail = text.rpartition(',')
        # one comma with one or two digits after it is a decimal comma ('3,5');
        # anything else - 45,000 or the Indian 12,00,000 - is grouping
        text = whole + '.' + tail if commas == 1 and len(tail) <= 2 else text.replace(',', '')
    elif dots:
        groups = text.split('.')
        # 45.000 or 1.200.000 is grouping; 2.5, 18.00 or 2.500 Lacs is a decimal
        if not has_unit and all(len(group) == 3 for group in groups[1:]):
            text = text.replace('.', '')
    try:
        return float(text)
    except ValueError:
        return None


def _currency(text: str) -> str | None:
    for pattern, code in CURRENCIES:
        match = pattern.search(text)
        if match:
            return code or match.group(1)
    return None


def _multiplier(suffix: str) -> int:
    for pattern, factor in MULTIPLIERS:
        if suffix and pattern.match(suffix):
            return factor
    return 1


def parse_salary(text: str | None) -> Salary | None:
    """'2-2.5 Lacs PA' -> Salary(200000, 250000, 'INR', 1). None when unparseable."""
    if not text:
        return None
    cleaned = text.replace('–', '-').replace('—', '-').replace('−', '-')
    if UNPRICED.search(cleaned):
        return None

    currency = _currency(cleaned)

    period = 1
    for pattern, factor in PERIODS:
        if pattern.search(cleaned):
            period = factor
            break

    matches = list(AMOUNT.finditer(cleaned))
    if not matches:
        return None
    # the first figure, and a second only when the text joins them as a range:
    # "$120,000 - $150,000 a year, plus 401k" is not a range up to 401,000
    taken = matches[:1]
    if len(matches) > 1 and RANGE.match(cleaned[matches[0].end() : matches[1].start()]):
        taken.append(matches[1])

    amounts = []
    for match in taken:
        raw, suffix = match.groups()
        multiplier = _multiplier(suffix)
        value = number(raw, has_unit=multiplier > 1)
        if value is not None:
            amounts.append((value, multiplier))
    if not amounts:
        return None

    # ranges carry the unit only on the last number ("2-2.5 Lacs"), so a bare
    # value in the same range takes the unit that was stated
    scale = max(multiplier for _, multiplier in amounts)
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
