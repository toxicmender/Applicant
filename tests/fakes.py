"""A scripted stand-in for a Playwright page, shared by the browser-flow tests.

The sources that drive a browser - Naukri, Google Jobs, Indeed's fallback,
Crunchbase and Tracxn on the web, Glassdoor, LinkedIn's signed-in flows - were
only ever run against the live sites. `FakePage` lets their own code run
against a script instead: which url answers with what status, html, body text
and elements, which API responses the page hears, and what scrolling reveals.

`patched_browser(page)` makes every `BrowserSession` - and so `browser()` -
start on that page, whichever way a module imports the launcher, and records
what was asked of the session.
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from playwright.sync_api import TimeoutError as PlaywrightTimeout

from applicant.boards import Capability
from applicant.infra.browser import BLOCKED_SELECTORS, BrowserSession


@dataclass
class Element:
    """One node: its text, its attributes, and what is inside it by selector.
    `raises`, when set, is what reading it raises - a node detached mid-read."""

    text: str = ''
    attrs: dict[str, str] = field(default_factory=dict)
    children: dict[str, list[Element]] = field(default_factory=dict)
    raises: BaseException | None = None
    checked: bool = False


@dataclass
class FakeResponse:
    """What `goto` returns, and what a page's 'response' handlers hear.
    `payload` is the JSON body, or an exception that reading it raises."""

    url: str = ''
    status: int = 200
    payload: Any = None

    def json(self) -> Any:
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


@dataclass
class Visit:
    """How one url answers."""

    status: int = 200
    html: str = ''
    body: str = ''
    title: str = ''
    elements: dict[str, list[Element]] = field(default_factory=dict)
    # API calls the page makes once loaded, heard by page.on('response')
    responses: list[FakeResponse] = field(default_factory=list)
    # where the browser ends up - a login wall, a checkpoint
    redirect: str | None = None
    # what each scroll appends: selector -> elements, one batch per wheel
    more: list[dict[str, list[Element]]] = field(default_factory=list)
    blocked: bool = False
    # goto itself fails: a timeout, a closed browser
    raises: BaseException | None = None


class FakeLocator:
    """Live, like Playwright's: one made by the page re-reads the page each time
    it is asked, so a locator held across a scroll sees what the scroll added."""

    def __init__(self, page: FakePage, selector: str, elements: list[Element] | None = None):
        self.page = page
        self.selector = selector
        self._fixed = elements

    @property
    def elements(self) -> list[Element]:
        if self._fixed is not None:
            return self._fixed
        return self.page._elements(self.selector)

    def count(self) -> int:
        return len(self.elements)

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator(self.page, self.selector, self.elements[index : index + 1])

    @property
    def first(self) -> FakeLocator:
        return self.nth(0)

    def _one(self) -> Element:
        if not self.elements:
            raise PlaywrightTimeout(f'no element for {self.selector!r}')
        element = self.elements[0]
        if element.raises is not None:
            raise element.raises
        return element

    def inner_text(self, **_: Any) -> str:
        return self._one().text

    def get_attribute(self, name: str, **_: Any) -> str | None:
        return self._one().attrs.get(name)

    def locator(self, selector: str) -> FakeLocator:
        found = [child for element in self.elements for child in element.children.get(selector, [])]
        return FakeLocator(self.page, selector, found)

    def or_(self, other: FakeLocator) -> FakeLocator:
        return FakeLocator(
            self.page, f'{self.selector} | {other.selector}', [*self.elements, *other.elements]
        )

    def wait_for(self, **_: Any) -> None:
        if not self.elements:
            raise PlaywrightTimeout(f'waiting for {self.selector!r}')

    def is_checked(self) -> bool:
        return self._one().checked

    def uncheck(self, **_: Any) -> None:
        self._one().checked = False

    def click(self, **_: Any) -> None:
        self._one()
        self.page.clicked.append(self.selector)


class FakePage:
    """A page that answers from `site`: (url pattern, Visit) pairs, the first
    pattern found in the url wins. A url nothing matches is a 404."""

    def __init__(self, site: list[tuple[str, Visit]] | None = None, *, url: str = 'about:blank'):
        self.site = list(site or [])
        self.url = url
        self.visit = Visit(status=0)
        self.visited: list[str] = []
        self.handlers: list[Any] = []
        self.filled: dict[str, str] = {}
        self.clicked: list[str] = []
        self.pressed: list[str] = []
        self.revealed = 0
        self.mouse = mock.Mock()
        self.mouse.wheel.side_effect = self._scroll
        self.keyboard = mock.Mock()
        self.keyboard.press.side_effect = self.pressed.append

    # -- navigation -------------------------------------------------------

    def goto(self, url: str, **_: Any) -> FakeResponse:
        self.visited.append(url)
        visit = next((v for pattern, v in self.site if re.search(pattern, url)), Visit(status=404))
        if visit.raises is not None:
            raise visit.raises
        self.visit, self.revealed = visit, 0
        self.url = visit.redirect or url
        for response in visit.responses:
            for handler in list(self.handlers):
                handler(response)
        return FakeResponse(url=url, status=visit.status)

    def on(self, event: str, handler: Any) -> None:
        if event == 'response':
            self.handlers.append(handler)

    # -- what is on it ----------------------------------------------------

    def title(self) -> str:
        return self.visit.title

    def content(self) -> str:
        return self.visit.html

    def inner_text(self, selector: str, **_: Any) -> str:
        return self.visit.body

    def _elements(self, selector: str) -> list[Element]:
        if selector == BLOCKED_SELECTORS:
            return [Element()] if self.visit.blocked else []
        found = list(self.visit.elements.get(selector, []))
        for batch in self.visit.more[: self.revealed]:
            found.extend(batch.get(selector, []))
        return found

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    def wait_for_selector(self, selector: str, **_: Any) -> FakeLocator:
        found = self.locator(selector)
        found.wait_for()
        return found

    # -- acting on it -----------------------------------------------------

    def fill(self, selector: str, value: str, **_: Any) -> None:
        self.filled[selector] = value

    def click(self, selector: str, **_: Any) -> None:
        self.clicked.append(selector)

    def _scroll(self, *_: Any) -> None:
        self.revealed = min(self.revealed + 1, len(self.visit.more))

    def wait_for_timeout(self, *_: Any) -> None:
        pass

    def wait_for_load_state(self, *_: Any, **__: Any) -> None:
        pass

    def set_default_timeout(self, *_: Any) -> None:
        pass


@dataclass
class Session:
    """What the code under test asked of its browser session."""

    started: int = 0
    closed: int = 0
    saved: list[Any] = field(default_factory=list)
    options: list[dict] = field(default_factory=list)


@contextmanager
def patched_browser(page: FakePage, saved_to: str = 'state.json') -> Iterator[Session]:
    """Every BrowserSession starts on `page`; nothing is launched."""
    session = Session()
    real_init = BrowserSession.__init__

    def init(self, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        session.options.append(
            {'profile_dir': self.profile_dir, 'storage_state': self.storage_state}
        )

    def start(self: BrowserSession) -> FakePage:
        session.started += 1
        return page

    def close(self: BrowserSession) -> None:
        session.closed += 1

    def save_state(self: BrowserSession, path: Any = None) -> str:
        session.saved.append(path)
        return str(path or saved_to)

    with (
        mock.patch.object(BrowserSession, '__init__', init),
        mock.patch.object(BrowserSession, 'start', start),
        mock.patch.object(BrowserSession, 'close', close),
        mock.patch.object(BrowserSession, 'save_state', save_state),
    ):
        yield session


class FakeBoard:
    """A board that answers every search with `result` - or raises it.

    What the Board protocol asks of every board: filters nothing, publishes nothing.
    """

    capability = Capability()

    def __init__(self, result):
        self.result = result
        self.closed = False

    def search(self, *args, **kwargs):
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


# -- a real browser, against saved pages -----------------------------------

PAGES = Path(__file__).parent / 'fixtures' / 'pages'
# the live sites declare UTF-8; without it Chromium reads '•' as windows-1252
HTML = 'text/html; charset=utf-8'
NOT_FOUND = b'<!doctype html><title>Not Found</title><p>Not Found</p>'
# why no browser can be launched (None: one can), worked out once per run
_LAUNCHABLE: dict[str, str | None] = {}


def chromium_executable() -> str | None:
    """The browser to use when the bundled build does not match this Playwright:
    APPLICANT_TEST_CHROMIUM, if set. None lets Playwright find its own."""
    return os.environ.get('APPLICANT_TEST_CHROMIUM') or None


class RealBrowserTest(unittest.TestCase):
    """A source's own code in a real Chromium, every request answered from
    tests/fixtures/pages - nothing reaches the network.

    `routes` maps a url pattern to (status, fixture path or None, extra headers).
    Skipped when no browser can be launched, unless APPLICANT_BROWSER_TESTS is
    set (CI's browser job), where that is a failure instead.
    """

    routes: ClassVar[tuple[tuple[str, tuple[int, str | None, dict[str, str]]], ...]] = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        reason = cls._launchable()
        if reason is not None:
            if os.environ.get('APPLICANT_BROWSER_TESTS'):
                raise RuntimeError(f'APPLICANT_BROWSER_TESTS is set but {reason}')
            raise unittest.SkipTest(reason)

    @classmethod
    def _launchable(cls) -> str | None:
        # asked once per run: a failed launch costs a second, per class
        if 'launch' not in _LAUNCHABLE:
            _LAUNCHABLE['launch'] = cls._try_launch()
        return _LAUNCHABLE['launch']

    @staticmethod
    def _try_launch() -> str | None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return 'playwright is not installed'
        try:
            with sync_playwright() as driver:
                driver.chromium.launch(executable_path=chromium_executable()).close()
        except Exception as error:  # noqa: BLE001 - any failure to launch means skip
            return 'no browser to launch ({}); set APPLICANT_TEST_CHROMIUM or run `uv run playwright install chromium`'.format(
                str(error).splitlines()[0][:120]
            )
        return None

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.requested: list[str] = []
        real_launch, real_start = BrowserSession._launch, BrowserSession.start
        test = self

        def launch(session, driver):
            # the bundled build only, from APPLICANT_TEST_CHROMIUM when set: an
            # installed Chrome would make the run depend on the machine
            session.channels = (None,)
            executable = chromium_executable()
            if executable is None:
                return real_launch(session, driver)
            chromium = driver.chromium
            wrapped = mock.Mock(wraps=chromium)
            wrapped.launch.side_effect = lambda **kw: chromium.launch(
                executable_path=executable, **kw
            )
            wrapped.launch_persistent_context.side_effect = lambda *a, **kw: (
                chromium.launch_persistent_context(*a, executable_path=executable, **kw)
            )
            return real_launch(session, mock.Mock(chromium=wrapped))

        def start(session):
            fresh = session._page is None
            page = real_start(session)
            if fresh:
                session.context.route('**/*', test._answer)
            return page

        for patch in (
            mock.patch.object(BrowserSession, '_launch', launch),
            mock.patch.object(BrowserSession, 'start', start),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _answer(self, route):
        url = route.request.url
        self.requested.append(url)
        for pattern, (status, fixture, headers) in self.routes:
            if re.search(pattern, url):
                # an error page has a body, as the live sites' do: Chromium fails
                # the navigation outright on an empty 4xx/5xx instead of returning it
                body = (PAGES / fixture).read_bytes() if fixture else NOT_FOUND
                kind = 'application/json' if fixture and fixture.endswith('.json') else HTML
                route.fulfill(status=status, body=body, content_type=kind, headers=headers)
                return
        route.fulfill(status=404, body=NOT_FOUND, content_type=HTML)
