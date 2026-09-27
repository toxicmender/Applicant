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
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.job import Job
from ..domain.ports import ApplicationResult
from ..errors import Blocked, SourceError
from ..infra.browser import BrowserSession
from ..interaction import Interaction, Terminal
from .linkedin import LinkedInGuest

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)


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
        applied = set(self.easy_apply(jobs))
        return [
            ApplicationResult(job, 'applied', 'linkedin easy apply')
            if job.url in applied
            else ApplicationResult(
                job, 'needs_manual_apply', 'not easy apply, or a multi step form'
            )
            for job in jobs
        ]

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

    def _session(self) -> BrowserContext:
        """The live context, started if it is not already."""
        self._start()
        if self._browser_session is None:  # pragma: no cover - _start always sets it
            raise SourceError('the browser session did not start')
        return self._browser_session.context

    def login(
        self,
        username,
        password,
        twoFA=False,
        filepath: str | Path = 'cookies.json',
        overwrite=False,
    ):
        target = Path(filepath)
        if target.exists() and not overwrite:
            logger.warning(
                '{} already exists. Pass overwrite to log in again, or use '
                'restore_session() to reuse it.'.format(filepath)
            )
            return None

        context = self._session()
        page = self._start()
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

        context.storage_state(path=str(target))
        logger.info(f'linkedin: signed in{" with 2FA" if twoFA else ""}; session saved to {target}')
        return target

    def restore_session(self, filepath: str | Path = 'cookies.json'):
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

    def scrape_jobs(self, filepath='job_listing.json'):
        """Recommended jobs from the signed in Jobs page."""
        from ..storage import save_jobs

        page = self._start()
        page.goto(
            'https://www.linkedin.com/jobs/collections/recommended/', wait_until='domcontentloaded'
        )
        if '/authwall' in page.url or '/login' in page.url:
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
                    easy_apply='Easy Apply' in card.inner_text(),
                )
            )

        total = save_jobs(jobs, filepath)
        logger.info(f'linkedin: {len(jobs)} recommended job(s) scraped, {total} in {filepath}')
        return jobs

    def easy_apply(self, source: str | Path | Iterable[Job | dict] = 'job_listing.json'):
        """Applies to the jobs that advertise Easy Apply. -> the urls applied to.

        `source` is the jobs themselves, or a job listing file to read them
        from. Only single step applications go through; anything asking extra
        questions is left open for you rather than guessed at.
        """
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

        applied = []
        for item in stored:
            if item.get('source') != 'linkedin' or not item.get('url'):
                continue
            if item.get('easy_apply') is False:
                continue
            # Each application is its own transaction. One that fails part way
            # must not take the ones already submitted with it: those are
            # returned, and so recorded, whatever happens to the rest
            # (OWASP Top 10:2025 A10 - roll back or complete, never lose track).
            try:
                if self._apply_one(page, item):
                    applied.append(item['url'])
            except Exception as error:  # one posting, logged in full, never fatal
                logger.error(
                    f'linkedin: easy apply failed for {item["url"]}: '
                    f'{type(error).__name__}: {error}'
                )
                logger.debug('linkedin: easy apply traceback', exc_info=True)

        logger.info(f'linkedin: easy applied to {len(applied)} job(s)')
        return applied

    def _apply_one(self, page, item) -> bool:
        """Submit one single-step Easy Apply form. True only once it is sent."""
        page.goto(item['url'], wait_until='domcontentloaded')
        button = page.locator('button.jobs-apply-button').first
        if not button.count():
            logger.debug(f'linkedin: no easy apply button on {item["url"]}')
            return False
        button.click()
        page.wait_for_timeout(1500)

        follow = page.locator('#follow-company-checkbox')
        if follow.count() and follow.is_checked():
            follow.uncheck(force=True)

        submit = page.locator('button[aria-label*="Submit application"]').first
        if submit.count():
            submit.click()
            page.wait_for_timeout(1500)
            logger.info(f'linkedin: applied to {item.get("title") or item["url"]} ({item["url"]})')
            return True

        # multi step form - close it and leave this one alone
        page.keyboard.press('Escape')
        logger.info(f'linkedin: left a multi step form open for review: {item["url"]}')
        return False

    # -- teardown ---------------------------------------------------------

    def close(self):
        """Release the browser, if one was started, and the HTTP client.

        Explicit rather than left to garbage collection: a `__del__` running at
        interpreter shutdown meets a Playwright that is already torn down.
        Safe to call more than once.
        """
        if self._browser_session is not None:
            self._browser_session.close()
            self._browser_session = None
        super().close()
