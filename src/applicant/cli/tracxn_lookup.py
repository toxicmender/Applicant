"""User-driven selection of a Tracxn profile from the financials CLI."""

from __future__ import annotations

import logging
import re
import signal
import sys
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..domain.providers import provider
from ..errors import AuthFailed, Blocked, ProfileBusy, SourceError, Unparseable
from ..financials import CompanyFinancials, TracxnClient
from ..financials.identity import company_name
from ..financials.tracker import has_financial_data
from ..financials.tracxn import (
    PLATFORM_PROFILE_URL,
    PROFILE_HOSTS,
    FinancialsInterrupted,
    TracxnLookupRequired,
    confirmed_profile_url,
    profile_match,
)
from ..infra.browser import STATE_FILE, BrowserSession, looks_blocked
from ..infra.browser_diagnostics import event, safe_origin, stage
from ..infra.owned_worker import CleanupUnconfirmed, WorkerFailure, run_worker
from ..infra.profile_lock import profile_lock
from ..infra.ranking_documents import read_ranking_document
from ..interaction import Terminal
from .render import shown

if TYPE_CHECKING:
    from collections.abc import Callable

    from playwright.sync_api import Frame, Page, Response

    from ..interaction import Interaction

logger = logging.getLogger(__name__)

WRONG_PAGE = re.compile(
    r'\b(?:no (?:matching )?results(?: found)?|page not found|404 not found|investor profile|'
    r'acquisition announcement|search results)\b',
    re.IGNORECASE,
)


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
        self.browser_factory = browser_factory
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

    def requires_lookup(self, company: str) -> bool:
        return self.client.requires_lookup(company)

    def close(self) -> None:
        session, self.session = self.session, None
        self.page = None
        if session is not None:
            session.close()

    def _open(self) -> None:
        from playwright.sync_api import Error

        self.crashed = False
        self.revision = 0
        self.document_status = None
        self.document_url = ''
        factory = self.browser_factory or BrowserSession
        self.session = factory(
            profile_dir=self.client.profile_dir,
            headless=False,
            timeout=int(self.client.timeout * 1000),
            require_saved_state=True,
        )
        try:
            page = self.session.start()
        except ProfileBusy:
            raise ProfileBusy(
                f'profile_busy: {self.client.profile_dir} is in use. '
                'Close the other Applicant run and retry.'
            ) from None
        except AuthFailed:
            raise SessionEnded(f'No saved Tracxn session. Run: {self.reauth}') from None
        except Error:
            raise LookupStopped('Could not open the Tracxn browser.') from None
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
            raise SessionEnded(f'needs_setup: Tracxn sign-in is required. Run: {self.reauth}')
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
            raise SessionEnded(f'needs_setup: Tracxn sign-in is required. Run: {self.reauth}')
        if self._status() in (403, 429):
            raise SessionEnded('Tracxn restricted browser access. Completed results were kept.')

    def _poll(self) -> None:
        assert self.page is not None
        if not self.page.is_closed():
            self.page.wait_for_timeout(50)
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
                    self.stopped = True
                    raise
        if self.browser_factory is None:
            return self._fetch_owned(company, max_rounds)
        try:
            return self._capture(company, max_rounds)
        finally:
            self.close()

    def _prompt(self, question, poll):
        if isinstance(self.interaction, Terminal):
            return self.interaction.ask_interruptibly(question, poll)
        return self.interaction.ask(question)

    def _fetch_owned(self, company, max_rounds):
        profile = Path(self.client.profile_dir or '.tx_profile').resolve()
        state_file = profile / STATE_FILE
        captures: list[CompanyFinancials] = []

        def progress(payload):
            captures.append(CompanyFinancials.model_validate(payload))

        try:
            with profile_lock(state_file.with_suffix('.lock')):
                if not state_file.is_file():
                    self.stopped = True
                    raise SessionEnded(f'needs_setup: No saved Tracxn session. Run: {self.reauth}')
                state = read_ranking_document(state_file)
                outcome, message = run_worker(
                    _lookup_worker,
                    (state, company, max_rounds, self.client.timeout, self.reauth, self.captured),
                    interactive=True,
                    prompt=self._prompt,
                    progress=progress,
                    provider='tracxn',
                )
        except ProfileBusy:
            self.stopped = True
            raise ProfileBusy(
                f'profile_busy: {profile} is in use. Close the other Applicant run and retry.'
            ) from None
        except KeyboardInterrupt:
            if captures:
                raise FinancialsInterrupted(captures[-1]) from None
            raise
        except WorkerFailure as error:
            self.stopped = True
            if isinstance(error, CleanupUnconfirmed):
                logger.error('%s: %s', profile, error)
                if isinstance(error.__cause__, KeyboardInterrupt):
                    if captures:
                        raise FinancialsInterrupted(captures[-1]) from None
                    raise KeyboardInterrupt from None
            if not captures:
                raise LookupStopped(f'Tracxn browser ended: {error}') from None
            logger.warning('Tracxn browser ended after capture: %s', error)
        else:
            if outcome == 'session':
                self.stopped = True
                raise SessionEnded(message)
            if outcome == 'stopped':
                self.stopped = True
                raise LookupStopped(message)
            if outcome == 'source':
                raise SourceError(message)

        if not captures:
            raise LookupStopped('The Tracxn worker did not return financials.')
        captured = captures[-1]
        if captured.company_id:
            self.captured.add(captured.company_id)
        return captured

    def _capture(self, company: str, max_rounds: int):
        from playwright.sync_api import Error

        try:
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
            if result.company_id != identity or not has_financial_data(result):
                raise Unparseable(
                    'Tracxn did not return usable financials for the confirmed company.'
                )
            self.captured.add(identity)
            return result
        except (LookupStopped, Blocked):
            self.stopped = True
            raise
        except Error as error:
            if type(error).__name__ == 'TargetClosedError' or (
                self.page is not None and self.page.is_closed()
            ):
                self.stopped = True
                raise LookupStopped(
                    'The Tracxn browser was closed. Completed results were kept.'
                ) from None
            if self.page is not None:
                try:
                    self._check_session()
                except (LookupStopped, Blocked):
                    self.stopped = True
                    raise
                except Error:
                    pass  # The page may still be navigating.
            raise SourceError(
                'Tracxn browser operation failed. See the run log for its stage.'
            ) from None


class _WorkerInteraction:
    def __init__(self, connection, check):
        self.connection = connection
        self.check = check

    def pause(self, message: str) -> None:
        self.ask(message)

    def ask(self, question: str, *, secret: bool = False) -> str:
        self.connection.send(('prompt', question))
        heartbeat = time.monotonic() + 0.25
        while not self.connection.poll(0):
            self.check()
            if time.monotonic() >= heartbeat:
                self.connection.send(('heartbeat', None))
                heartbeat = time.monotonic() + 0.25
        answer = self.connection.recv()
        if answer is None:
            raise LookupStopped('Tracxn lookup was cancelled.')
        return answer


def _lookup_worker(connection, arguments):
    state, company, max_rounds, timeout, reauth, previously_captured = arguments
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # The parent owns terminal cancellation.
    client = TracxnClient(api_key='', profile_dir=None, timeout=timeout)

    def check():
        resolver._poll()

    resolver = InteractiveTracxn(
        client,
        _WorkerInteraction(connection, check),
        reauth,
        browser_factory=partial(BrowserSession, storage_state=state),
    )
    resolver.captured.update(previously_captured)
    outcome: tuple[str, str | None] = ('complete', None)
    try:
        financials = resolver._capture(company, max_rounds)
        connection.send(('progress', financials.to_dict()))
    except SessionEnded as error:
        outcome = 'session', str(error)
    except LookupStopped as error:
        outcome = 'stopped', str(error)
    except SourceError as error:
        outcome = 'source', str(error)
    finally:
        try:
            connection.send(('closing', None))
        finally:
            resolver.close()
    return outcome
