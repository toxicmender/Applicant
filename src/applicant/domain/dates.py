"""Turning the many ways a board states a posting date into an ISO one."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

RELATIVE = re.compile(r'(\d+)\+?\s*(minute|hour|day|week|month)s?\s*ago', re.IGNORECASE)
UNITS = {'minute': 1 / 1440.0, 'hour': 1 / 24.0, 'day': 1.0, 'week': 7.0, 'month': 30.0}


def relative_to_iso(text: str | None, now: datetime | None = None) -> str | None:
    """'6 days ago' -> '2026-07-28'. Returns None when the text is not relative."""
    if not text:
        return None
    match = RELATIVE.search(text)
    if not match:
        return None
    days = int(match.group(1)) * UNITS[match.group(2).lower()]
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).date().isoformat()


def epoch_to_iso(value: object) -> str | None:
    """Indeed hands out epoch milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None
