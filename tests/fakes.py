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

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
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
