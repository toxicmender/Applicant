"""Turning the many ways a board states a posting date into an ISO one."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

RELATIVE = re.compile(
    r'(\d+|\b(?:an?|few|some)\b)\+?\s*(minute|hour|day|week|month)s?\s*ago', re.IGNORECASE
)
UNITS = {'minute': 1 / 1440.0, 'hour': 1 / 24.0, 'day': 1.0, 'week': 7.0, 'month': 30.0}
# the words boards use for the newest postings, in days before today. Google
# Jobs says "Just posted", Naukri "Just Now" and "Today" - without these, the
# freshest jobs are the ones a date filter cannot place
TODAY = re.compile(r'\b(just\s+(?:now|posted)|today|yesterday)\b', re.IGNORECASE)


def relative_to_iso(text: str | None, now: datetime | None = None) -> str | None:
    """'6 days ago' -> '2026-07-28'; 'Today', 'Just now', 'a day ago', 'Few hours
    ago' too. Returns None when the text is not relative."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    match = RELATIVE.search(text)
    if match:
        count = match.group(1).lower()
        # "a day ago" is one; "few hours ago" is still today, one unit is enough
        number = int(count) if count.isdigit() else 1
        days = number * UNITS[match.group(2).lower()]
        return (now - timedelta(days=days)).date().isoformat()
    word = TODAY.search(text)
    if word:
        days = 1 if word.group(1).lower() == 'yesterday' else 0
        return (now - timedelta(days=days)).date().isoformat()
    return None


def epoch_to_iso(value: object) -> str | None:
    """Indeed hands out epoch milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None
