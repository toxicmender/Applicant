"""Launching a browser the boards will actually talk to."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from .models import JobsError

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from playwright.sync_api import Page

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
)

BROWSER_ARGS = ['--disable-blink-features=AutomationControlled', '--disable-extensions']

# Akamai and friends fingerprint Playwright's bundled Chromium and return
# "Access Denied" outright, while an installed Chrome or Edge sails through.
# None means the bundled build, which is the last resort.
CHANNELS = ('chrome', 'msedge', None)

BLOCKED_TITLES = ('just a moment', 'attention required', 'unusual traffic', 'access denied')
BLOCKED_SELECTORS = '#challenge-platform, #cf-challenge-running, form#captcha-form'


@contextmanager
def browser(profile_dir=None, headless=True, timeout=45000, channels=CHANNELS) -> Iterator[Page]:
    """A Playwright page, persistent when a profile dir is given.

    A persistent profile is what lets a board remember that a human already
    cleared its bot check.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise JobsError(
            'this source needs playwright: uv sync && uv run playwright install chromium'
        ) from None

    options = {'headless': headless, 'args': BROWSER_ARGS}
    context_options = {
        'user_agent': USER_AGENT,
        'locale': 'en-US',
        'viewport': {'width': 1366, 'height': 900},
    }

    with sync_playwright() as driver:
        closer = context = None
        failures = []

        for channel in channels:
            extra = {'channel': channel} if channel else {}
            try:
                if profile_dir:
                    context = driver.chromium.launch_persistent_context(
                        profile_dir, **dict(options, **context_options, **extra)
                    )
                    closer = context
                else:
                    instance = driver.chromium.launch(**dict(options, **extra))
                    context = instance.new_context(**context_options)
                    closer = instance
                break
            except Exception as error:  # noqa: BLE001 - the channel is simply not installed
                failures.append(
                    '{}: {}'.format(channel or 'bundled', str(error).split('\n')[0][:80])
                )

        if context is None or closer is None:
            raise JobsError('could not launch a browser -\n  ' + '\n  '.join(failures))

        page = context.pages[0] if profile_dir and context.pages else context.new_page()
        page.set_default_timeout(timeout)
        try:
            yield page
        finally:
            closer.close()


def looks_blocked(page) -> bool:
    """Cheap shared check for the interstitials these boards throw."""
    try:
        title = (page.title() or '').lower()
    except Exception:  # noqa: BLE001 - a page mid-navigation has no title yet
        return False
    if any(flag in title for flag in BLOCKED_TITLES):
        return True
    return page.locator(BLOCKED_SELECTORS).count() > 0
