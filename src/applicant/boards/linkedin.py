"""LinkedIn jobs, on Playwright.

Two very different paths live here:

* `search()` uses LinkedIn's guest endpoint, which serves job cards to logged
  out clients. No browser and no account needed, so it is the reliable one.
* `login()` / `scrape_jobs()` / `easy_apply()` drive a real browser against the
  logged in site, which is the only way to reach recommended jobs and the Easy
  Apply flow.

Session state is Playwright's storage_state. Cookie files written by the older
Selenium version are converted on read, so an existing cookies.json keeps working.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ..infra.browser import BrowserSession
from ..infra.http import HEADERS, HttpClient
from ..models import BlockedError, Job, JobsError
from . import CAPABILITIES

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

GUEST_SEARCH = 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search'
GUEST_POSTING = 'https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{}'
GUEST_PAGE_SIZE = 10

CARD = re.compile(r'<li>(.*?)</li>', re.DOTALL)
FIELDS = {
    'id': re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"'),
    'url': re.compile(r'<a[^>]+href="([^"?]+)'),
    'title': re.compile(r'base-search-card__title">\s*(.*?)\s*</h3', re.DOTALL),
    'company': re.compile(r'hidden-nested-link"[^>]*>\s*(.*?)\s*</a', re.DOTALL),
    'location': re.compile(r'job-search-card__location">\s*(.*?)\s*</span', re.DOTALL),
    'posted': re.compile(r'datetime="([^"]+)"'),
}
TAGS = re.compile(r'<[^>]+>')


def _clean(value):
    if value is None:
        return None
    return TAGS.sub('', value).replace('&amp;', '&').strip() or None


class LinkedIn:
    capability = CAPABILITIES['linkedin']

    def __init__(
        self,
        path=None,
        headless=True,
        state='linkedin_state.json',
        timeout=45000,
        delay=1.0,
        client=None,
    ):
        # `path` was the chromedriver location under Selenium; Playwright ships its
        # own browser, so it is accepted only so old call sites keep working
        self.driver_path = path
        self.headless = headless
        self.state = state
        self.timeout = timeout
        self.delay = delay
        # 429 is deliberately not retried: carrying on through a rate limit is
        # how a working scrape becomes a blocked one, so it is Blocked at once.
        # `delay` spaces every guest request, search pages and posting reads alike.
        self.http = HttpClient(
            'LinkedIn',
            client=client,
            headers=HEADERS,
            backoff=delay,
            interval=delay,
            retry_on=(502, 503, 504),
        )
        self.client = self.http.client
        self._browser_session: BrowserSession | None = None

    # -- guest search (no account needed) ---------------------------------

    def search(self, keywords, location='', limit=25, posted_within_days=None):
        jobs = []
        seen = set()
        start = 0

        while len(jobs) < limit:
            query = {'keywords': keywords, 'location': location, 'start': start}
            if posted_within_days:
                # LinkedIn wants the window in seconds, as r<seconds>
                query['f_TPR'] = 'r{}'.format(int(posted_within_days) * 86400)
            logger.debug(f'linkedin: guest search page at offset {start}')
            # an unreachable host or a 429 leaves HttpClient as a JobsError
            response = self.http.get('{}?{}'.format(GUEST_SEARCH, urlencode(query)))
            if response.is_error:
                raise JobsError(f'LinkedIn guest search answered HTTP {response.status_code}')

            cards = CARD.findall(response.text)
            if not cards:
                break

            before = len(jobs)
            for card in cards:
                job = self._card_to_job(card)
                if job is None or job.id in seen:
                    continue
                seen.add(job.id)
                jobs.append(job)
                if len(jobs) >= limit:
                    break

            # the guest endpoint re-serves the last page once `start` runs past
            # the end of the results, so a page of nothing new means we are done
            if len(jobs) == before:
                break

            start += GUEST_PAGE_SIZE

        logger.info(f'linkedin: {min(len(jobs), limit)} job(s) from the guest search')
        return jobs[:limit]

    def _card_to_job(self, card):
        found = {}
        for name, pattern in FIELDS.items():
            match = pattern.search(card)
            found[name] = match.group(1) if match else None

        title = _clean(found['title'])
        if not title:
            return None

        return Job(
            source='linkedin',
            id=found['id'],
            title=title,
            company=_clean(found['company']),
            location=_clean(found['location']),
            url=found['url'],
            posted=found['posted'],
            easy_apply=None,  # the guest card does not say
        )

    def describe(self, job: Job) -> str | None:
        """The posting's own text, for what its search card never said.

        The same guest surface `search` uses, so this needs no account and no
        browser. Returns None when there is nothing to read - no id, or a page
        that is not there - and raises on a rate limit, because carrying on
        through one is how a working scrape becomes a blocked one.
        """
        if not job.id:
            return None

        # a 429 leaves as Blocked, which stops the enrichment rather than this job
        response = self.http.get(GUEST_POSTING.format(job.id))
        if response.status_code != 200:
            return None

        text = unescape(TAGS.sub(' ', response.text))
        return re.sub(r'\s+', ' ', text).strip() or None

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
            raise JobsError('the browser session did not start')
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
            return

        context = self._session()
        page = self._start()
        page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded')
        page.fill('#username', username)
        page.fill('#password', password)
        page.click('button[type="submit"]')

        if twoFA:
            page.wait_for_selector('input[name="pin"], #input__phone_verification_pin')
            page.fill('input[name="pin"], #input__phone_verification_pin', input('Enter OTP: '))
            page.click('#two-step-submit-button, button[type="submit"]')

        page.wait_for_load_state('domcontentloaded')
        # authentication outcomes are logged (ASVS 16.3.1); who and with what
        # password never are - this is the user's own account on their machine
        if '/login' in page.url or '/checkpoint/' in page.url:
            logger.warning(f'linkedin: sign in did not complete (stopped at {page.url})')
            raise BlockedError(
                'LinkedIn did not complete the login (still on {}). '
                'A manual challenge is probably waiting - rerun with '
                'headless disabled.'.format(page.url)
            )

        context.storage_state(path=str(target))
        logger.info(f'linkedin: signed in{" with 2FA" if twoFA else ""}; session saved to {target}')
        print('session saved to {}'.format(target))

    def restore_session(self, filepath: str | Path = 'cookies.json'):
        state = self._load_state(filepath)
        if state is None:
            logger.warning(f'linkedin: no usable session in {filepath}')
            print('no usable session in {}; call login() first'.format(filepath))
            return False

        page = self._start(storage_state=state)
        page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded')
        if '/login' in page.url or '/authwall' in page.url:
            logger.warning(f'linkedin: the session in {filepath} has expired')
            print('saved session is no longer valid; call login() again')
            return False
        logger.info(f'linkedin: session restored from {filepath}')
        print('session restored from {}'.format(filepath))
        return True

    def _load_state(self, filepath: str | Path):
        """Accepts Playwright storage_state or the old Selenium cookies.json."""
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

        cookies = payload.get('list') if isinstance(payload, dict) else None
        if not cookies:
            return None

        converted = []
        for cookie in cookies:
            try:
                if not cookie.get('name'):
                    continue
                converted.append(
                    {
                        'name': cookie['name'],
                        'value': cookie.get('value', ''),
                        'domain': cookie.get('domain', '.linkedin.com'),
                        'path': cookie.get('path', '/'),
                        'expires': float(cookie.get('expiry', -1)),
                        'httpOnly': bool(cookie.get('httpOnly')),
                        'secure': bool(cookie.get('secure', True)),
                        'sameSite': 'Lax',
                    }
                )
            except (AttributeError, TypeError, ValueError) as error:
                # one malformed cookie; its value is a credential, so only the
                # error type is logged
                logger.warning(f'linkedin: skipped a malformed cookie ({type(error).__name__})')
        logger.info(f'linkedin: converted {len(converted)} Selenium cookie(s)')
        print('converted {} Selenium cookies to a Playwright session'.format(len(converted)))
        return {'cookies': converted, 'origins': []}

    # -- logged in flows --------------------------------------------------

    def scrape_jobs(self, filepath='job_listing.json'):
        """Recommended jobs from the signed in Jobs page."""
        from ..storage import save_jobs

        page = self._start()
        page.goto(
            'https://www.linkedin.com/jobs/collections/recommended/', wait_until='domcontentloaded'
        )
        if '/authwall' in page.url or '/login' in page.url:
            raise BlockedError('not signed in - call login() or restore_session() first')

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
        logger.info(f'linkedin: {len(jobs)} recommended job(s) scraped')
        print('scraped {} recommended jobs ({} in {})'.format(len(jobs), total, filepath))
        return jobs

    def easy_apply(self, filepath='job_listing.json'):
        """Applies to the stored jobs that advertise Easy Apply.

        Only single step applications go through; anything asking extra questions
        is left open for you rather than guessed at.
        """
        page = self._start()
        try:
            with open(filepath, encoding='utf-8') as file:
                stored = json.load(file).get('list', [])
        except (FileNotFoundError, ValueError) as error:
            logger.warning(f'linkedin: could not read {filepath}: {error}')
            print('could not read {}: {}'.format(filepath, error))
            return []

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
                print('failed: {}'.format(item.get('title') or item['url']))

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
            logger.info(f'linkedin: applied to {item["url"]}')
            print('applied: {}'.format(item.get('title') or item['url']))
            return True

        # multi step form - close it and leave this one alone
        page.keyboard.press('Escape')
        logger.info(f'linkedin: left a multi step form open for review: {item["url"]}')
        print('skipped (multi step): {}'.format(item.get('title') or item['url']))
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
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
