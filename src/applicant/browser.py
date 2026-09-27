"""Kept so `from applicant.browser import browser` goes on working.

The launcher lives in `applicant.infra.browser` now, shared by every source.
"""

from .infra.browser import (
    BLOCKED_SELECTORS,
    BLOCKED_TITLES,
    BROWSER_ARGS,
    CHANNELS,
    BrowserSession,
    browser,
    looks_blocked,
)
from .infra.http import USER_AGENT

__all__ = [
    'BLOCKED_SELECTORS',
    'BLOCKED_TITLES',
    'BROWSER_ARGS',
    'CHANNELS',
    'USER_AGENT',
    'BrowserSession',
    'browser',
    'looks_blocked',
]
