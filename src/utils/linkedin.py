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
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .jobs import USER_AGENT, BROWSER_ARGS, BlockedError, Job, JobsError, relative_to_iso

GUEST_SEARCH = 'https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search'
GUEST_PAGE_SIZE = 10

HEADERS = {'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'}

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
    def __init__(self, path=None, headless=True, state='linkedin_state.json', timeout=45000):
        # `path` was the chromedriver location under Selenium; Playwright ships its
        # own browser, so it is accepted only so old call sites keep working
        self.driver_path = path
        self.headless = headless
        self.state = state
        self.timeout = timeout
        self.client = httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True)
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # -- guest search (no account needed) ---------------------------------

    def search(self, keywords, location='', limit=25):
        jobs = []
        seen = set()
        start = 0

        while len(jobs) < limit:
            query = {'keywords': keywords, 'location': location, 'start': start}
            response = self.client.get('{}?{}'.format(GUEST_SEARCH, urlencode(query)))
            if response.status_code == 429:
                raise BlockedError('LinkedIn rate limited the guest search; slow down or retry later')
            response.raise_for_status()

            cards = CARD.findall(response.text)
            if not cards:
                break

            for card in cards:
                job = self._card_to_job(card)
                if job is None or job.id in seen:
                    continue
                seen.add(job.id)
                jobs.append(job)
                if len(jobs) >= limit:
                    break

            start += GUEST_PAGE_SIZE
            if len(jobs) < limit:
                time.sleep(1.0)

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

    # -- browser session --------------------------------------------------

    def _start(self, storage_state=None):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise JobsError('the LinkedIn browser flows need playwright: '
                            'uv sync && uv run playwright install chromium')

        if self._context is not None:
            return self._page

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless,
                                                         args=BROWSER_ARGS)
        self._context = self._browser.new_context(
            user_agent=USER_AGENT, locale='en-US',
            viewport={'width': 1366, 'height': 900},
            storage_state=storage_state)
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout)
        return self._page

    def login(self, username, password, twoFA=False, filepath='cookies.json', overwrite=False):
        target = Path(filepath)
        if target.exists() and not overwrite:
            print('{} already exists. Pass overwrite to log in again, or use '
                  'restore_session() to reuse it.'.format(filepath))
            return

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
        if '/login' in page.url or '/checkpoint/' in page.url:
            raise BlockedError('LinkedIn did not complete the login (still on {}). '
                               'A manual challenge is probably waiting - rerun with '
                               'headless disabled.'.format(page.url))

        self._context.storage_state(path=str(target))
        print('session saved to {}'.format(target))

    def restore_session(self, filepath='cookies.json'):
        state = self._load_state(filepath)
        if state is None:
            print('no usable session in {}; call login() first'.format(filepath))
            return False

        page = self._start(storage_state=state)
        page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded')
        if '/login' in page.url or '/authwall' in page.url:
            print('saved session is no longer valid; call login() again')
            return False
        print('session restored from {}'.format(filepath))
        return True

    def _load_state(self, filepath):
        """Accepts Playwright storage_state or the old Selenium cookies.json."""
        try:
            with open(filepath, 'r', encoding='utf-8') as file:
                payload = json.load(file)
        except (FileNotFoundError, ValueError):
            return None

        if isinstance(payload, dict) and 'cookies' in payload:
            return payload

        cookies = payload.get('list') if isinstance(payload, dict) else None
        if not cookies:
            return None

        converted = []
        for cookie in cookies:
            if not cookie.get('name'):
                continue
            converted.append({
                'name': cookie['name'],
                'value': cookie.get('value', ''),
                'domain': cookie.get('domain', '.linkedin.com'),
                'path': cookie.get('path', '/'),
                'expires': float(cookie.get('expiry', -1)),
                'httpOnly': bool(cookie.get('httpOnly')),
                'secure': bool(cookie.get('secure', True)),
                'sameSite': 'Lax',
            })
        print('converted {} Selenium cookies to a Playwright session'.format(len(converted)))
        return {'cookies': converted, 'origins': []}

    # -- logged in flows --------------------------------------------------

    def scrape_jobs(self, filepath='job_listing.json'):
        """Recommended jobs from the signed in Jobs page."""
        from .jobs import save_jobs

        page = self._start()
        page.goto('https://www.linkedin.com/jobs/collections/recommended/',
                  wait_until='domcontentloaded')
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
            except Exception:
                continue
            if not text:
                continue

            job_id = card.get_attribute('data-job-id')
            link = card.locator('a[href*="/jobs/view/"]').first
            url = link.get_attribute('href') if link.count() else None
            if url and url.startswith('/'):
                url = 'https://www.linkedin.com' + url

            jobs.append(Job(
                source='linkedin',
                id=job_id,
                title=text[0],
                company=text[1] if len(text) > 1 else None,
                location=text[2] if len(text) > 2 else None,
                url=url.split('?')[0] if url else None,
                easy_apply='Easy Apply' in card.inner_text(),
            ))

        total = save_jobs(jobs, filepath)
        print('scraped {} recommended jobs ({} in {})'.format(len(jobs), total, filepath))
        return jobs

    def easy_apply(self, filepath='job_listing.json'):
        """Applies to the stored jobs that advertise Easy Apply.

        Only single step applications go through; anything asking extra questions
        is left open for you rather than guessed at.
        """
        page = self._start()
        try:
            with open(filepath, 'r', encoding='utf-8') as file:
                stored = json.load(file).get('list', [])
        except (FileNotFoundError, ValueError) as error:
            print('could not read {}: {}'.format(filepath, error))
            return []

        applied = []
        for item in stored:
            if item.get('source') != 'linkedin' or not item.get('url'):
                continue
            if item.get('easy_apply') is False:
                continue

            page.goto(item['url'], wait_until='domcontentloaded')
            button = page.locator('button.jobs-apply-button').first
            if not button.count():
                continue
            button.click()
            page.wait_for_timeout(1500)

            follow = page.locator('#follow-company-checkbox')
            if follow.count() and follow.is_checked():
                follow.uncheck(force=True)

            submit = page.locator('button[aria-label*="Submit application"]').first
            if submit.count():
                submit.click()
                page.wait_for_timeout(1500)
                applied.append(item['url'])
                print('applied: {}'.format(item.get('title') or item['url']))
            else:
                # multi step form - close it and leave this one alone
                page.keyboard.press('Escape')
                print('skipped (multi step): {}'.format(item.get('title') or item['url']))

        return applied

    # -- teardown ---------------------------------------------------------

    def close(self):
        for handle in ('_context', '_browser'):
            target = getattr(self, handle, None)
            if target is not None:
                try:
                    target.close()
                except Exception:
                    pass
                setattr(self, handle, None)
        if getattr(self, '_playwright', None) is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def __del__(self):
        self.close()
