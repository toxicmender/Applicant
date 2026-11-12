"""Company name comparison for Tracxn lookup and saved history."""

import re
import unicodedata

LEGAL_SUFFIXES = (
    ('private', 'limited'),
    ('pvt', 'ltd'),
    ('l', 'l', 'c'),
    ('inc',),
    ('incorporated',),
    ('llc',),
    ('corp',),
    ('corporation',),
    ('ltd',),
    ('limited',),
    ('llp',),
    ('plc',),
)
STOCK_LABEL = re.compile(r'\s+\([A-Z0-9][A-Z0-9./-]{0,14}:[A-Z0-9][A-Z0-9.-]{0,14}\)$')


def company_name(value: str) -> tuple[str, ...]:
    normalized = STOCK_LABEL.sub('', unicodedata.normalize('NFKC', value).strip())
    normalized = normalized.casefold().replace('&', ' and ')
    words = tuple(''.join(char if char.isalnum() else ' ' for char in normalized).split())
    while suffix := next(
        (item for item in LEGAL_SUFFIXES if len(words) > len(item) and words[-len(item) :] == item),
        (),
    ):
        words = words[: -len(suffix)]
    return words
