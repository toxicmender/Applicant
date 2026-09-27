"""The one place a browser is launched.

There used to be three: `applicant.browser.browser()`, LinkedIn's own
long-lived session, and Glassdoor's own persistent profile. Only the first
tried an installed Chrome or Edge before the bundled Chromium, which matters:
Akamai and friends fingerprint Playwright's bundled build and answer "Access
Denied" outright, while an installed Chrome sails through. Now every source
gets that fallback, the same stealth options, and a guaranteed close.

`BrowserSession` is the long-lived form (LinkedIn keeps one open from sign in
to Easy Apply); `browser()` is the everyday one-page form built on it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import SourceError
from .http import USER_AGENT

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from playwright.sync_api import BrowserContext, Page, Playwright

BROWSER_ARGS = ['--disable-blink-features=AutomationControlled', '--disable-extensions']

# Installed Chrome, then Edge, then the bundled build as the last resort (None).
CHANNELS: tuple[str | None, ...] = ('chrome', 'msedge', None)

BLOCKED_TITLES = ('just a moment', 'attention required', 'unusual traffic', 'access denied')
BLOCKED_SELECTORS = '#challenge-platform, #cf-challenge-running, form#captcha-form'

# where a profile's signed in state is exported, inside the profile itself
STATE_FILE = 'storage_state.json'

logger = logging.getLogger(__name__)


class BrowserSession:
    """A Playwright browser, context and page, started once and closed once.

    With `profile_dir` the context is persistent, which is what lets a site
    remember that a human already cleared its bot check. Without one it is
    fresh, optionally seeded from a saved `storage_state` (a signed in session).
    """

    def __init__(
        self,
        profile_dir: str | os.PathLike[str] | None = None,
        headless: bool = True,
        timeout: int = 45000,
        channels: Sequence[str | None] = CHANNELS,
        storage_state: Any = None,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout = timeout
        self.channels = tuple(channels)
        self.storage_state = storage_state
        self._playwright: Playwright | None = None
        self._closer: Any = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def context(self) -> BrowserContext:
        self.start()
        if self._context is None:  # pragma: no cover - start() sets it or raises
            raise SourceError('the browser session did not start')
        return self._context

    @property
    def page(self) -> Page:
        return self.start()

    def start(self) -> Page:
        """The session's page, launching the browser on first use."""
        if self._page is not None:
            return self._page

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SourceError(
                'this source needs playwright: uv sync && uv run playwright install chromium'
            ) from None

        self._playwright = sync_playwright().start()
        try:
            context = self._launch(self._playwright)
            page = context.pages[0] if self.profile_dir and context.pages else context.new_page()
            page.set_default_timeout(self.timeout)
        except BaseException:
            self.close()
            raise
        self._context, self._page = context, page
        return page

    def _launch(self, driver: Playwright) -> BrowserContext:
        options: dict[str, Any] = {'headless': self.headless, 'args': BROWSER_ARGS}
        context_options: dict[str, Any] = {
            'user_agent': USER_AGENT,
            'locale': 'en-US',
            'viewport': {'width': 1366, 'height': 900},
        }
        failures = []

        for channel in self.channels:
            extra: dict[str, Any] = {'channel': channel} if channel else {}
            try:
                if self.profile_dir:
                    context = driver.chromium.launch_persistent_context(
                        str(self.profile_dir), **options, **context_options, **extra
                    )
                    self._closer = context
                else:
                    instance = driver.chromium.launch(**options, **extra)
                    self._closer = instance
                    context = instance.new_context(
                        **context_options, storage_state=self.storage_state
                    )
            except Exception as error:  # noqa: BLE001 - the channel is simply not installed
                self._close_quietly('_closer')
                logger.debug(f'browser: {channel or "bundled"} unavailable: {error}')
                failures.append(
                    '{}: {}'.format(channel or 'bundled', str(error).split('\n')[0][:80])
                )
                continue
            logger.debug(
                f'browser: launched {channel or "bundled chromium"}'
                + (f' with profile {self.profile_dir}' if self.profile_dir else '')
            )
            return context

        logger.error(f'browser: no browser could be launched ({len(failures)} tried)')
        raise SourceError('could not launch a browser -\n  ' + '\n  '.join(failures))

    def save_state(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Export the signed in state (cookies, local storage) to a file.

        By default into the profile directory, beside the profile it came from,
        rather than into whatever directory the command happened to run in. It
        is a session credential, so the file is owner-only.
        """
        if path is None:
            if not self.profile_dir:
                raise SourceError('no profile directory to save the session state into')
            path = Path(self.profile_dir) / STATE_FILE
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # created 0600 before Playwright writes into it; an older file saved with
        # looser permissions is tightened too
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT, 0o600))
        os.chmod(target, 0o600)
        self.context.storage_state(path=str(target))
        return target

    def close(self) -> None:
        """Close everything that was opened. Safe to call more than once."""
        self._close_quietly('_closer')
        self._close_quietly('_playwright')
        self._context = self._page = None

    def _close_quietly(self, name: str) -> None:
        handle = getattr(self, name)
        setattr(self, name, None)
        if handle is None:
            return
        try:
            if name == '_playwright':
                handle.stop()
            else:
                handle.close()
        except Exception as error:  # noqa: BLE001 - teardown is best effort
            logger.debug(f'browser: closing {name[1:]} failed: {error}')

    def __enter__(self) -> BrowserSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


@contextmanager
def browser(
    profile_dir: str | os.PathLike[str] | None = None,
    headless: bool = True,
    timeout: int = 45000,
    channels: Sequence[str | None] = CHANNELS,
) -> Iterator[Page]:
    """A Playwright page for the length of a `with` block."""
    with BrowserSession(profile_dir, headless, timeout, channels) as session:
        yield session.start()


def looks_blocked(page) -> bool:
    """Cheap shared check for the interstitials these sites throw."""
    try:
        title = (page.title() or '').lower()
    except Exception:  # noqa: BLE001 - a page mid-navigation has no title yet
        return False
    if any(flag in title for flag in BLOCKED_TITLES):
        logger.info(f'browser: bot check detected by page title {title!r}')
        return True
    if page.locator(BLOCKED_SELECTORS).count() > 0:
        logger.info('browser: bot check detected by challenge element')
        return True
    return False
