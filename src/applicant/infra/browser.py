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

import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import copy_context
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..errors import AuthFailed, SourceError
from ..permissions import make_private
from .browser_diagnostics import event, network_code, safe_origin, stage
from .http import USER_AGENT
from .profile_lock import profile_lock
from .ranking_documents import read_ranking_document

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from playwright.sync_api import BrowserContext, Page, Playwright

BROWSER_ARGS = ['--disable-blink-features=AutomationControlled', '--disable-extensions']

# Installed Chrome, then Edge, then the bundled build as the last resort (None).
CHANNELS: tuple[str | None, ...] = ('chrome', 'msedge', None)

BLOCKED_TITLES = ('just a moment', 'attention required', 'unusual traffic', 'access denied')
BLOCKED_SELECTORS = '#challenge-platform, #cf-challenge-running, form#captcha-form'

# where a profile's signed in state is exported, inside the profile itself
STATE_FILE = 'storage_state.json'


def _observe_page(page: Page) -> None:
    from playwright.sync_api import Error

    # Sync Playwright can dispatch events on a different greenlet.
    notify = copy_context().run

    def failed_request(request):
        code = network_code(request.failure or '')
        if code:
            notify(event, 'request', 'failed', origin=safe_origin(request.url), network_code=code)

    def response_received(response):
        try:
            main_document = (
                response.status >= 400
                and response.request.is_navigation_request()
                and response.frame == page.main_frame
            )
        except Error:  # Playwright may not have an associated frame yet.
            return
        if main_document:
            notify(
                event,
                'navigation',
                'http_error',
                origin=safe_origin(response.url),
                status=response.status,
            )

    page.on('requestfailed', failed_request)
    page.on('response', response_received)
    page.on('crash', lambda *_: notify(event, 'page', 'crashed'))


def _observe_browser(browser) -> None:
    if browser is not None:
        notify = copy_context().run
        browser.on('disconnected', lambda *_: notify(event, 'browser', 'disconnected'))


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
        require_saved_state: bool = False,
    ):
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout = timeout
        self.channels = tuple(channels)
        self.storage_state = storage_state
        self.require_saved_state = require_saved_state
        self._playwright: Playwright | None = None
        self._closer: Any = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._profile_lock: Any = None

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

        try:
            if self.profile_dir:
                state = Path(self.profile_dir) / STATE_FILE
                self._profile_lock = profile_lock(state.with_suffix('.lock'))
                self._profile_lock.__enter__()
                if state.is_file() and self.storage_state is None:
                    self.storage_state = read_ranking_document(state)
            if self.require_saved_state and self.storage_state is None:
                raise AuthFailed('no saved browser session')
            with stage('playwright_startup'):
                self._playwright = sync_playwright().start()
            context = self._launch(self._playwright)
            with stage('page_creation'):
                page = (
                    context.pages[0] if self.profile_dir and context.pages else context.new_page()
                )
                page.set_default_timeout(self.timeout)
                _observe_page(page)
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
            label = channel if channel in ('chrome', 'msedge', 'chromium') else 'bundled'
            try:
                with stage('browser_launch', channel=label, headless=self.headless):
                    if self.profile_dir and self.storage_state is None:
                        with stage('context_creation', persistent=True):
                            context = driver.chromium.launch_persistent_context(
                                str(self.profile_dir), **options, **context_options, **extra
                            )
                            self._closer = context
                            _observe_browser(context.browser)
                    else:
                        instance = driver.chromium.launch(**options, **extra)
                        self._closer = instance
                        _observe_browser(instance)
                        with stage('context_creation', persistent=False):
                            context = instance.new_context(
                                **context_options, storage_state=self.storage_state
                            )
            except Exception as error:  # noqa: BLE001 - try the next browser channel
                self._close_quietly('_closer')
                failures.append(f'{label}: {type(error).__name__}')
                continue
            return context

        with stage('browser_launch'):
            raise SourceError('could not launch a browser - ' + '; '.join(failures))

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
        self.start()
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # created 0600 before Playwright writes into it; an older file saved with
        # looser permissions is tightened too
        os.close(os.open(target, os.O_WRONLY | os.O_CREAT, 0o600))
        make_private(target)
        self.context.storage_state(path=str(target))
        return target

    def close(self) -> None:
        """Close everything that was opened. Safe to call more than once."""
        self._close_quietly('_closer')
        self._close_quietly('_playwright')
        self._context = self._page = None
        if self._profile_lock is not None:
            lock, self._profile_lock = self._profile_lock, None
            lock.__exit__(None, None, None)

    def _close_quietly(self, name: str) -> None:
        handle = getattr(self, name)
        setattr(self, name, None)
        if handle is None:
            return
        try:
            with stage('browser_teardown', component=name[1:]):
                if name == '_playwright':
                    handle.stop()
                else:
                    handle.close()
        except Exception:  # noqa: BLE001 - teardown is best effort and already logged
            pass

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
        event('challenge_detection', 'blocked', marker='title')
        return True
    if page.locator(BLOCKED_SELECTORS).count() > 0:
        event('challenge_detection', 'blocked', marker='element')
        return True
    return False
