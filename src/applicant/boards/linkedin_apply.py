"""LinkedIn as you: signing in, recommended jobs, and Easy Apply.

The only module that acts on a real account. Kept apart from the guest search
(`applicant.boards.linkedin`) because the risks differ: a bad job card costs a
wrong row in a CSV, a bad form submission costs an application sent in your
name. `search` never imports this module; `apply` loads it only to submit.

`LinkedIn.apply(jobs, dry_run=...)` is the `Applier` port the apply service
calls. `dry_run` has no default: whoever calls it has to say which they mean,
and a dry run never opens a browser.

Session state is Playwright's storage_state, saved by `login`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from ..domain.job import Job
from ..domain.ports import ApplicationResult, Interrupted, easy_apply_results
from ..errors import ApplicationBlocked, Blocked, SourceError
from ..infra.browser import BrowserSession, looks_blocked
from ..infra.profile_lock import profile_lock
from ..interaction import Interaction, Terminal
from .linkedin import LinkedInGuest

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from ..infra.store.repositories import Backend
    from ..services.application_attempts import AttemptJournal

logger = logging.getLogger(__name__)

# What LinkedIn shows once an application has gone through, and what it shows
# when a form refused it. Kept here, together, because they are the first thing
# to update when LinkedIn changes its markup.
APPLICATION_SENT = 'text=/application (was )?(sent|submitted)/i'
FORM_ERROR = '.artdeco-inline-feedback--error'
CONFIRM_TIMEOUT_MS = 10_000
# what _apply_one says when it clicked Submit and heard nothing back
UNCONFIRMED_SUBMIT = 'unconfirmed'
# where LinkedIn sends a browser that is not signed in, or must pass a check
SIGNED_OUT = ('/login', '/authwall', '/checkpoint/', '/uas/')


_SUBMIT_GUARD = """button => {
    const document = button.ownerDocument;
    const view = document.defaultView;
    const path = node => {
        const result = [];
        for (; node; node = node.parentNode || node.host) result.push(node);
        return result;
    };
    const chain = path(button);
    const modal = chain.find(node => node.matches?.('[role="dialog"][aria-modal="true"]'));
    const url = view.location.href;
    const form = button.form;
    const action = button.getAttribute('formaction') ?? form?.getAttribute('action');
    const active = () => modal && button.isConnected && view.location.href === url
        && !button.disabled && button.getAttribute('aria-disabled') !== 'true'
        && !path(button).some(node => node.matches?.('[inert],[aria-hidden="true"]'))
        && path(button).length === chain.length && path(button).every((node, i) => node === chain[i])
        && button.form === form
        && (button.getAttribute('formaction') ?? form?.getAttribute('action')) === action;
    let clicked = false;
    let rejected = false;
    const events = ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'];
    const check = event => {
        if (rejected || !active() || !event.composedPath().includes(button) || clicked) {
            rejected = true;
            event.preventDefault();
            event.stopImmediatePropagation();
        } else if (event.type === 'click') {
            clicked = true;
        }
    };
    if (!active()) return null;
    for (const event of events) document.addEventListener(event, check, true);
    return {
        active,
        clicked: () => clicked && !rejected,
        close: () => {
            for (const event of events) document.removeEventListener(event, check, true);
        }
    };
}"""


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme, parts.hostname, parts.port


def _linkedin_url(url):
    scheme, host, port = _origin(url)
    return (
        scheme == 'https'
        and port in (None, 443)
        and bool(host and (host == 'linkedin.com' or host.endswith('.linkedin.com')))
    )


def _prepare_submit(page):
    """Locate and trial a single final control before arming an attempt."""
    from playwright.sync_api import Error

    origin = _origin(page.url)
    candidates = []
    for frame in page.frames:
        if frame != page.main_frame and _origin(frame.url) != origin:
            continue
        dialogs = frame.locator('[role="dialog"][aria-modal="true"]').filter(visible=True)
        if not dialogs.count():
            continue
        modal = dialogs.last
        if modal.get_by_text(re.compile(r'\bStep\s+\d+\s+of\s+\d+\b', re.IGNORECASE)).count():
            return None
        if any(
            modal.get_by_role('button', name=label, exact=True).count()
            for label in ('Next', 'Review')
        ):
            return None
        submit = modal.get_by_role('button', name='Submit application', exact=True)
        if submit.count() == 1 and submit.is_visible() and submit.is_enabled():
            candidates.append(submit)
    if len(candidates) != 1:
        return None
    submit = candidates[0]
    try:
        button = submit.element_handle(timeout=1500)
        submit.click(trial=True, timeout=1500)
    except Error:
        return None
    if button is None:
        return None
    route = page.url

    def dispatch():
        guard = None
        try:
            if page.url != route or not submit.evaluate(
                '(element, original) => element === original', button
            ):
                return False
            guard = button.evaluate_handle(_SUBMIT_GUARD)
            if not guard.evaluate('guard => guard?.active() ?? false'):
                return False
            bounds = button.bounding_box()
            if bounds is None or not guard.evaluate('guard => guard.active()'):
                return False
            page.mouse.click(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
            if not guard.evaluate('guard => guard.clicked()'):
                raise SourceError('submit input reached the page without a verified click')
            return True
        finally:
            if guard is not None:
                with suppress(Error):
                    guard.evaluate('guard => guard?.close()')
                    guard.dispose()

    return dispatch


class LinkedIn(LinkedInGuest):
    """The guest search, plus everything that needs you signed in."""

    source = 'linkedin'

    def __init__(
        self,
        headless=True,
        timeout=45000,
        delay=1.0,
        client=None,
        interaction: Interaction | None = None,
    ):
        super().__init__(headless=headless, timeout=timeout, delay=delay, client=client)
        # asked for the one-time code when a sign in needs two factors
        self.interaction = interaction or Terminal()
        self._browser_session: BrowserSession | None = None
        self._session_lock = None
        self._session_path = None
        # the last easy_apply's outcomes, kept as they happen so an interrupt
        # cannot lose them: the urls applied to, and those submitted without
        # LinkedIn confirming them - possibly sent, so never retried blindly -
        # and those that failed before anything was sent (url -> why): not an
        # outcome, so a later run tries them again
        self.applied: list[str] = []
        self.unconfirmed: list[str] = []
        self.failed: dict[str, str] = {}
        self.attempt_journal: AttemptJournal | None = None

    # -- the Applier port -------------------------------------------------

    def apply(self, jobs: list[Job], *, dry_run: bool) -> list[ApplicationResult]:
        """Easy Apply to each job, or - with `dry_run` - say what would be done.

        A dry run touches nothing: no browser, no request. Otherwise only
        single step forms are submitted; the rest come back as needing a
        person. Failures propagate - the apply service records them as
        `failed` rows rather than losing the other boards' results.
        """
        if dry_run:
            return [ApplicationResult(job, 'would_apply', 'dry run') for job in jobs]
        try:
            applied = self.easy_apply(jobs)
        except KeyboardInterrupt as stop:
            done = easy_apply_results(
                jobs, self.applied, self.unconfirmed, self.failed, tried_only=True
            )
            raise Interrupted(done) from stop
        return easy_apply_results(jobs, set(applied), set(self.unconfirmed), self.failed)

    # -- browser session --------------------------------------------------

    def _start(self, storage_state=None) -> Page:
        """The signed in flows' page, launched on first use.

        Through the shared launcher, so it gets the same installed-Chrome-first
        fallback as every other source rather than the bundled Chromium only.
        """
        if self._browser_session is None:
            self._browser_session = BrowserSession(
                headless=self.headless, timeout=self.timeout, storage_state=storage_state
            )
        return self._browser_session.start()

    def _session(self) -> BrowserSession:
        """The live browser session, started if it is not already."""
        self._start()
        if self._browser_session is None:  # pragma: no cover - _start always sets it
            raise SourceError('the browser session did not start')
        return self._browser_session

    def login(
        self,
        username: str,
        password: str,
        twoFA: bool = False,
        filepath: str | Path = 'cookies.json',
        overwrite: bool = False,
    ) -> Path | None:
        target = Path(filepath)
        self._claim_session(target)
        try:
            if target.exists() and not overwrite:
                logger.warning(
                    '{} already exists. Pass overwrite to log in again, or use '
                    'restore_session() to reuse it.'.format(filepath)
                )
                if self._browser_session is None:
                    self._close_session()
                return None
            return self._sign_in(target, username, password, twoFA)
        except BaseException:
            self._close_session()
            raise

    def _sign_in(self, target: Path, username: str, password: str, twoFA: bool) -> Path:
        session = self._session()
        page = session.page
        page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')
        page.fill('#username', username)
        page.fill('#password', password)
        page.click('button[type="submit"]')

        if twoFA:
            page.wait_for_selector('input[name="pin"], #input__phone_verification_pin')
            code = self.interaction.ask('Enter OTP: ', secret=True)
            page.fill('input[name="pin"], #input__phone_verification_pin', code)
            page.click('#two-step-submit-button, button[type="submit"]')

        page.wait_for_load_state('domcontentloaded')
        # authentication outcomes are logged (ASVS 16.3.1); who and with what
        # password never are - this is the user's own account on their machine
        if '/login' in page.url or '/checkpoint/' in page.url:
            logger.warning(f'linkedin: sign in did not complete (stopped at {page.url})')
            raise Blocked(
                'LinkedIn did not complete the login (still on {}). '
                'A manual challenge is probably waiting - rerun with '
                'headless disabled.'.format(page.url)
            )

        # owner-only, and the directory made if need be: it is a session credential
        target = session.save_state(target)
        logger.info(f'linkedin: signed in{" with 2FA" if twoFA else ""}; session saved to {target}')
        return target

    def restore_session(self, filepath: str | Path = 'cookies.json'):
        self._claim_session(Path(filepath))
        try:
            if self._restore_session(filepath):
                return True
        except BaseException:
            self._close_session()
            raise
        self._close_session()
        return False

    def _restore_session(self, filepath: str | Path) -> bool:
        state = self._load_state(filepath)
        if state is None:
            logger.warning(f'linkedin: no usable session in {filepath}; call login() first')
            return False

        page = self._start(storage_state=state)
        page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded')
        if '/login' in page.url or '/authwall' in page.url:
            logger.warning(f'linkedin: the session in {filepath} has expired; call login() again')
            return False
        logger.info(f'linkedin: session restored from {filepath}')
        return True

    def _claim_session(self, path):
        path = Path(path).resolve()
        if self._session_lock is not None:
            if self._session_path != path:
                raise SourceError('close the current session before selecting another profile')
            return
        lock = profile_lock(path.with_suffix('.lock'))
        lock.__enter__()
        self._session_lock, self._session_path = lock, path

    def _load_state(self, filepath: str | Path):
        """A Playwright storage_state saved by `login`, or None."""
        try:
            with open(filepath, encoding='utf-8') as file:
                payload = json.load(file)
        except FileNotFoundError:
            return None
        except ValueError as error:
            logger.warning(f'linkedin: {filepath} is not valid JSON ({error})')
            return None

        if isinstance(payload, dict) and 'cookies' in payload:
            return payload

        # A Selenium-era cookies.json ({'list': [...]}) was converted until 0.2.0;
        # now it is simply not a session, and signing in again writes one
        logger.warning(
            f'linkedin: {filepath} is not a saved session (an old Selenium cookie file?); '
            'sign in again to replace it'
        )
        return None

    # -- logged in flows --------------------------------------------------

    def scrape_jobs(
        self,
        filepath: str | None = 'job_listing.json',
        backend: Backend = 'files',
        *,
        storage_state: dict[str, Any] | None = None,
    ) -> list[Job]:
        """Recommended jobs from the signed in Jobs page, merged into `filepath`
        through the store `backend` names."""
        from ..infra.store.repositories import listing

        page = (
            self._start(storage_state=storage_state) if storage_state is not None else self._start()
        )
        page.goto(
            'https://www.linkedin.com/jobs/collections/recommended/', wait_until='domcontentloaded'
        )
        if any(wall in page.url for wall in SIGNED_OUT):
            raise Blocked('not signed in - call login() or restore_session() first')

        cards = page.locator('[data-job-id], .job-card-container')
        seen = 0
        # the list is virtualised, so keep scrolling until it stops growing
        for _ in range(30):
            count = cards.count()
            if count and count == seen:
                break
            seen = count
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1200)

        jobs = []
        for index in range(cards.count()):
            card = cards.nth(index)
            try:
                text = [line for line in card.inner_text().split('\n') if line.strip()]
            except Exception:  # noqa: BLE001 - a virtualised card scrolled out of the DOM
                continue
            if not text:
                continue

            job_id = card.get_attribute('data-job-id')
            link = card.locator('a[href*="/jobs/view/"]').first
            url = link.get_attribute('href') if link.count() else None
            if url and url.startswith('/'):
                url = 'https://www.linkedin.com' + url

            jobs.append(
                Job(
                    source='linkedin',
                    id=job_id,
                    title=text[0],
                    company=text[1] if len(text) > 1 else None,
                    location=text[2] if len(text) > 2 else None,
                    url=url.split('?')[0] if url else None,
                    # from the text already read: the card may be gone by now
                    easy_apply=any('Easy Apply' in line for line in text),
                )
            )

        if filepath is not None and jobs:
            total = listing(filepath, backend).save(jobs)
            logger.info(f'linkedin: {len(jobs)} recommended job(s) scraped, {total} in {filepath}')
        return jobs

    def easy_apply(self, source: str | Path | Iterable[Job | dict] = 'job_listing.json'):
        """Applies to the jobs that advertise Easy Apply. -> the urls applied to.

        `source` is the jobs themselves, or a job listing file to read them
        from. Only single step applications go through; anything asking extra
        questions is left open for you rather than guessed at.
        """
        self.applied, self.unconfirmed, self.failed = [], [], {}
        if isinstance(source, (str, Path)):
            try:
                with open(source, encoding='utf-8') as file:
                    stored = json.load(file).get('list', [])
            except (FileNotFoundError, ValueError) as error:
                logger.warning(f'linkedin: could not read {source}: {error}')
                return []
        else:
            stored = [item.to_dict() if isinstance(item, Job) else item for item in source]
        if not stored:
            return []
        page = self._start()

        applied = self.applied
        eligible = [
            item
            for item in stored
            if item.get('source') == 'linkedin'
            and item.get('url')
            and item.get('easy_apply') is not False
        ]
        for index, item in enumerate(eligible):
            # Each application is its own transaction. One that fails part way
            # must not take the ones already submitted with it: those are
            # returned, and so recorded, whatever happens to the rest
            # (OWASP Top 10:2025 A10 - roll back or complete, never lose track).
            try:
                outcome = self._apply_one(page, item)
                if outcome == UNCONFIRMED_SUBMIT:
                    self.unconfirmed.append(item['url'])
                elif outcome:
                    applied.append(item['url'])
            except Blocked as error:
                # signed out, or a check to clear: nothing more can be done in
                # this run, and none of what is left has an outcome yet
                logger.warning(f'linkedin: stopped applying: {error}')
                for left in eligible[index:]:
                    self.failed[left['url']] = str(error)
                break
            except ApplicationBlocked as error:
                logger.warning('linkedin: %s', error)
                self.failed[item['url']] = f'{error.status}: {error}'
            except Exception as error:  # one posting, logged in full, never fatal
                logger.error(
                    f'linkedin: easy apply failed for {item["url"]}: '
                    f'{type(error).__name__}: {error}'
                )
                logger.debug('linkedin: easy apply traceback', exc_info=True)
                # a timeout or a crashed page is not an outcome: tried again later
                self.failed[item['url']] = type(error).__name__

        logger.info(f'linkedin: easy applied to {len(applied)} job(s)')
        return applied

    def _apply_one(self, page, item) -> bool | str:
        """Submit one single-step Easy Apply form. True once LinkedIn confirms it;
        UNCONFIRMED_SUBMIT when Submit was clicked and no confirmation came - it
        may have gone through; False when nothing was submitted."""
        if not _linkedin_url(item['url']):
            return False
        page.goto(item['url'], wait_until='domcontentloaded')
        # a login wall has no Easy Apply button either - but it is no answer
        # about this job, and every job after it would read the same
        if any(wall in page.url for wall in SIGNED_OUT) or looks_blocked(page):
            raise Blocked(
                'LinkedIn signed out, or asked for a check, at {} - run `applicant jobs` '
                '(with --show to clear a check) and apply again'.format(page.url)
            )
        button = page.locator('button.jobs-apply-button').first
        if not button.count():
            logger.debug(f'linkedin: no easy apply button on {item["url"]}')
            return False
        button.click()
        page.wait_for_timeout(1500)

        follow = page.locator('#follow-company-checkbox')
        if follow.count() and follow.is_checked():
            follow.uncheck(force=True)

        from ..services.application_attempts import linkedin_posting_key

        if not _linkedin_url(page.url) or linkedin_posting_key(item) != linkedin_posting_key(
            {'url': page.url}
        ):
            return False
        submit = _prepare_submit(page)
        if submit is not None:
            journal = self.attempt_journal
            attempt = journal.begin(item, armed=True) if journal is not None else None
            # from here on the application may have been sent, whatever happens
            try:
                if not submit():
                    if journal is not None and attempt is not None:
                        journal.resolve(attempt, 'failed-before-dispatch')
                    return False
                confirmed = self._confirmed(page)
            except KeyboardInterrupt:
                # stopped while waiting for LinkedIn's answer
                self.unconfirmed.append(item['url'])
                raise
            except Exception as error:  # noqa: BLE001 - the click may have sent it
                logger.warning(
                    f'linkedin: {type(error).__name__} after submitting {item["url"]}: {error}'
                )
                confirmed = False
            if confirmed:
                if journal is not None and attempt is not None:
                    journal.resolve(attempt, 'submitted')
                logger.info(
                    f'linkedin: applied to {item.get("title") or item["url"]} ({item["url"]})'
                )
                return True
            if journal is not None and attempt is not None:
                journal.resolve(attempt, 'uncertain')
            # a click is not an application: a required field, a hiccup, and
            # LinkedIn keeps the form open. Logged as applied, it would never
            # be offered again - so it is left for a person to finish
            with suppress(Exception):  # a page that is gone has no form to close
                page.keyboard.press('Escape')
            logger.warning(f'linkedin: submitted but not confirmed, left for review: {item["url"]}')
            return UNCONFIRMED_SUBMIT

        # multi step form - close it and leave this one alone
        page.keyboard.press('Escape')
        logger.info(f'linkedin: left a multi step form open for review: {item["url"]}')
        return False

    def _confirmed(self, page) -> bool:
        """Whether LinkedIn confirmed the application, rather than refused it."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        sent = page.locator(APPLICATION_SENT)
        try:
            sent.or_(page.locator(FORM_ERROR)).first.wait_for(timeout=CONFIRM_TIMEOUT_MS)
        except PlaywrightTimeout:
            return False
        return sent.count() > 0

    # -- teardown ---------------------------------------------------------

    def _close_session(self):
        if self._browser_session is not None:
            self._browser_session.close()
            self._browser_session = None
        if self._session_lock is not None:
            lock, self._session_lock = self._session_lock, None
            self._session_path = None
            lock.__exit__(None, None, None)

    def close(self):
        """Release the browser and its profile lock, then close the HTTP client."""
        self._close_session()
        super().close()
