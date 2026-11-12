"""User-driven selection of a Tracxn profile from the financials CLI."""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..domain.providers import provider
from ..errors import AuthFailed, Blocked, ProfileBusy, SourceError, Unparseable
from ..financials.tracxn import (
    PLATFORM_PROFILE_URL,
    PROFILE_HOSTS,
    TracxnLookupRequired,
    confirmed_profile_url,
    profile_match,
)
from ..infra.browser import BrowserSession, looks_blocked
from ..infra.browser_diagnostics import event, safe_origin, stage
from ..interaction import Terminal
from .render import shown

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import Frame, Page, Response

    from ..financials import TracxnClient
    from ..interaction import Interaction

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
WRONG_PAGE = re.compile(
    r'\b(?:no (?:matching )?results(?: found)?|page not found|404 not found|investor profile|'
    r'acquisition announcement|search results)\b',
    re.IGNORECASE,
)
STOCK_LABEL = re.compile(r'\s+\([A-Z0-9][A-Z0-9./-]{0,14}:[A-Z0-9][A-Z0-9.-]{0,14}\)$')


def company_name(value: str) -> tuple[str, ...]:
    normalized = STOCK_LABEL.sub('', unicodedata.normalize('NFKC', value).strip())
    normalized = normalized.casefold().replace('&', ' and ')
    words = tuple(''.join(char if char.isalnum() else ' ' for char in normalized).split())
    for suffix in LEGAL_SUFFIXES:
        if len(words) > len(suffix) and words[-len(suffix) :] == suffix:
            return words[: -len(suffix)]
    return words


def terminal_available() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


class LookupStopped(SourceError):
    """The user ended lookup or its browser was closed."""


class SessionEnded(Blocked):
    """A saved session expired or the browser reached an access restriction."""


@dataclass(frozen=True)
class Selection:
    url: str
    name: str
    title: str
    revision: int


class InteractiveTracxn:
    def __init__(
        self,
        client: TracxnClient,
        interaction: Interaction,
        reauth: str,
        *,
        eligible: Callable[[], bool] | None = None,
        browser_factory: Callable[..., BrowserSession] | None = None,
    ):
        self.client = client
        self.interaction = interaction
        self.reauth = reauth
        self.eligible = eligible or terminal_available
        self.browser_factory = browser_factory or BrowserSession
        self.session: BrowserSession | None = None
        self.page: Page | None = None
        self.offered = False
        self.stopped = False
        self.crashed = False
        self.revision = 0
        self.document_status: int | None = None
        self.document_url = ''
        self.captured: set[str] = set()

    def accepts(self, company: str) -> bool:
        return self.client.accepts(company)

    def close(self) -> None:
        session, self.session = self.session, None
        self.page = None
        if session is not None:
            session.close()

    def _stop(self) -> None:
        self.stopped = True
        self.close()

    def _open(self) -> None:
        from playwright.sync_api import Error

        self.session = self.browser_factory(
            profile_dir=self.client.profile_dir,
            headless=False,
            timeout=int(self.client.timeout * 1000),
            require_saved_state=True,
        )
        try:
            page = self.session.start()
        except ProfileBusy:
            self._stop()
            raise ProfileBusy(
                f'profile_busy: {self.client.profile_dir} is in use. '
                'Close the other Applicant run and retry.'
            ) from None
        except AuthFailed:
            raise SessionEnded(f'No saved Tracxn session. Run: {self.reauth}') from None
        except Error:
            raise LookupStopped('Could not open the Tracxn browser.') from None
        except SourceError:
            self._stop()
            raise
        self.page = page

        def on_response(response: Response) -> None:
            request = response.request
            if request.is_navigation_request():
                # A navigation can be reported before Playwright associates its frame.
                try:
                    main_document = response.frame == page.main_frame
                except Error:
                    return
                if main_document:
                    self.document_status = response.status
                    self.document_url = response.url.split('#', 1)[0]

        def on_navigation(frame: Frame) -> None:
            if frame == page.main_frame:
                self.revision += 1

        def on_crash(_page: Page) -> None:
            self.crashed = True

        page.on('response', on_response)
        page.on('framenavigated', on_navigation)
        page.on('crash', on_crash)
        dashboard = provider('tracxn').check_url
        assert dashboard is not None
        try:
            with stage('tracxn_lookup_start'):
                page.goto(dashboard, wait_until='domcontentloaded')
                self._check_session()
                if urlsplit(page.url).hostname not in PROFILE_HOSTS:
                    raise SessionEnded(
                        f'Tracxn redirected away from its dashboard. Run: {self.reauth}'
                    )
                if looks_blocked(page):
                    raise SessionEnded('Tracxn restricted browser access.')
        except Error:
            raise LookupStopped('Could not load the Tracxn dashboard. See the run log.') from None

    def _status(self) -> int | None:
        assert self.page is not None
        return self.document_status if self.page.url.split('#', 1)[0] == self.document_url else None

    def _check_session(self) -> None:
        assert self.page is not None
        if self.page.is_closed():
            raise LookupStopped('The Tracxn browser was closed. Completed results were kept.')
        if self.crashed:
            raise LookupStopped('The Tracxn tab crashed. Completed results were kept.')
        address = urlsplit(self.page.url)
        if address.hostname in ('accounts.google.com', 'login.microsoftonline.com'):
            raise SessionEnded(f'Tracxn sign-in is required. Run: {self.reauth}')
        if address.hostname not in PROFILE_HOSTS:
            return
        if (
            re.search(r'/(?:login|signin|sign-in|authwall)(?:/|$)', address.path, re.I)
            or self.page.locator(
                ':is(form[action*="login"], input[type="password"], input[type="email"]'
                '[placeholder*="business" i]):visible'
            ).count()
            or self._status() == 401
        ):
            raise SessionEnded(f'Tracxn sign-in is required. Run: {self.reauth}')
        if self._status() in (403, 429):
            raise SessionEnded('Tracxn restricted browser access. Completed results were kept.')

    def _poll(self) -> None:
        assert self.page is not None
        if not self.page.is_closed():
            self.page.wait_for_timeout(100)
        self._check_session()

    def _ask(self, prompt: str) -> str:
        try:
            if isinstance(self.interaction, Terminal):
                return self.interaction.ask_interruptibly(prompt, self._poll).strip().lower()
            self._check_session()
            answer = self.interaction.ask(prompt)
            self._check_session()
            return answer.strip().lower()
        except EOFError:
            raise LookupStopped('Tracxn lookup input ended. Completed results were kept.') from None

    def _company_headings(self, url: str, title: str) -> list[str]:
        assert self.page is not None
        platform = PLATFORM_PROFILE_URL.fullmatch(url)
        if platform is not None:
            links = self.page.locator(
                'a.txn--dp-profile-card__company-name[href]:visible'
            ).evaluate_all('(links) => links.map(link => [link.href, link.innerText])')
            if links:
                names = []
                for href, name in links:
                    destination = PLATFORM_PROFILE_URL.fullmatch(confirmed_profile_url(href) or '')
                    if destination is not None and destination['id'] == platform['id']:
                        names.append(name)
                return names
        headings = self.page.locator(
            '[data-testid="company-profile-name"]:visible'
        ).all_text_contents()
        if not headings and ('company profile' in title.casefold() or platform is not None):
            headings = self.page.locator('h1:visible').all_text_contents()
            if not headings:
                headings = self.page.get_by_role('heading', level=1).all_text_contents()
        return headings

    def _selection(self, company: str) -> Selection:
        assert self.page is not None
        self._check_session()
        address = self.page.url
        url = confirmed_profile_url(address)
        status = self._status()
        tabs = len(self.page.context.pages)
        path = urlsplit(address).path
        route = 'company_profile' if url else 'dashboard' if path == '/a/dashboard' else 'other'
        event(
            'tracxn_profile_inspection',
            'started',
            origin=safe_origin(address),
            route=route,
            open_tabs=tabs,
            http_status=status,
        )
        if url is None:
            event('tracxn_profile_inspection', 'rejected', reason='unsupported_profile_url')
            message = 'Open a Tracxn company profile before confirming.'
            if tabs > 1:
                message += (
                    ' Applicant is inspecting the tab it opened. Use that tab for the profile.'
                )
            raise Unparseable(message)
        if status is not None and status >= 400:
            event('tracxn_profile_inspection', 'rejected', reason='http_error', http_status=status)
            raise Unparseable(f'Tracxn returned HTTP {status} for this profile.')
        if looks_blocked(self.page):
            raise SessionEnded('Tracxn restricted browser access. Completed results were kept.')
        title = self.page.title()
        names = [text.strip() for text in self._company_headings(url, title) if text.strip()]
        wrong_page = any(WRONG_PAGE.search(x) for x in [title, *names])
        if not names or len(set(names)) != 1 or wrong_page:
            event(
                'tracxn_profile_inspection',
                'rejected',
                reason='wrong_page' if wrong_page else 'missing_or_ambiguous_heading',
                heading_count=len(set(names)),
                company_profile_title='company profile' in title.casefold(),
            )
            raise Unparseable('The current page does not identify a single company profile.')
        if not company_name(names[0]) or company_name(names[0]) != company_name(company):
            event('tracxn_profile_inspection', 'rejected', reason='company_name_mismatch')
            raise Unparseable(
                f'Profile heading {shown(names[0])!r} does not match {shown(company)!r}.'
            )
        event('tracxn_profile_inspection', 'matched')
        return Selection(url, names[0], title, self.revision)

    def _select(self, company: str) -> Selection:
        feedback = ''
        while True:
            reply = self._ask(
                f'{feedback}Search for {shown(company)!r} in Tracxn and open its profile. '
                'Press Enter to inspect, or type skip or stop.'
            )
            feedback = ''
            self._handle_command(reply, company)
            if reply not in ('', 'back'):
                feedback = 'Use Enter, skip, or stop. '
                continue
            if reply == 'back':
                continue
            try:
                selection = self._selection(company)
                answer = self._ask(
                    f'Requested: {shown(company)}\nProfile: {shown(selection.name)}\n'
                    f'Tab title: {shown(selection.title)}\nURL: {shown(selection.url)}\n'
                    'Type yes to read this profile, back to search again, skip, or stop.'
                )
                self._handle_command(answer, company)
                if answer == 'yes':
                    if self._selection(company) == selection:
                        event('tracxn_profile_confirmation', 'accepted')
                        return selection
                    event('tracxn_profile_confirmation', 'rejected', reason='selection_changed')
                if answer != 'back':
                    feedback = 'Selection was not confirmed. '
            except Unparseable as error:
                feedback = f'{error} '

    @staticmethod
    def _handle_command(reply: str, company: str) -> None:
        if reply == 'stop':
            raise LookupStopped('Tracxn lookup stopped. Completed results were kept.')
        if reply == 'skip':
            raise SourceError(f'Skipped Tracxn lookup for {shown(company)!r}.')

    def fetch(self, company: str, max_rounds: int = 20):
        from playwright.sync_api import Error

        try:
            if self.page is not None and (profile := profile_match(company.strip())):
                return self.client.fetch_open_profile(
                    profile.group('base'), self.page, max_rounds, check=self._check_session
                )
            try:
                return self.client.fetch(company, max_rounds=max_rounds)
            except TracxnLookupRequired:
                if self.stopped or not self.eligible():
                    raise
                if not self.offered:
                    self.offered = True
                    try:
                        accepted = (
                            self.interaction.ask(
                                'Look up companies in your saved Tracxn browser session? '
                                'Continue if your account permits browser extraction. [y/N] '
                            )
                            .strip()
                            .lower()
                        )
                    except EOFError:
                        accepted = ''
                    if accepted not in ('y', 'yes'):
                        self._stop()
                        raise
                    self._open()
            with stage('tracxn_profile_selection'):
                selected = self._select(company)
            profile = profile_match(selected.url)
            assert profile is not None
            identity = profile['id']
            if identity in self.captured:
                raise SourceError(f'This Tracxn profile was already recorded: {selected.url}')
            assert self.page is not None
            with stage('tracxn_profile_capture'):
                result = self.client.fetch_open_profile(
                    selected.url,
                    self.page,
                    max_rounds,
                    check=self._check_session,
                    selected_name=selected.name,
                )
            if company_name(result.company) != company_name(selected.name):
                raise Unparseable('Captured company identity does not match the confirmed profile')
            self.captured.add(identity)
            return result
        except (LookupStopped, Blocked):
            self._stop()
            raise
        except Error as error:
            if type(error).__name__ == 'TargetClosedError' or (
                self.page is not None and self.page.is_closed()
            ):
                self._stop()
                raise LookupStopped(
                    'The Tracxn browser was closed. Completed results were kept.'
                ) from None
            if self.page is not None:
                try:
                    self._check_session()
                except (LookupStopped, Blocked):
                    self._stop()
                    raise
                except Error:
                    pass  # The page may still be navigating.
            raise SourceError(
                'Tracxn browser operation failed. See the run log for its stage.'
            ) from None
