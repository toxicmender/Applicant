"""Naukri job search.

Naukri's `jobapi/v3/search` endpoint answers 406 "recaptcha required" to any
request we build ourselves - the params alone are not enough, the site's own
JS adds a header we cannot reproduce. So the browser is pointed at a normal
search page and its own successful API call is intercepted on the way past.

If that call does not appear, the rendered job tuples are parsed instead;
either path yields the same Job objects.
"""

from __future__ import annotations

import re

from .jobs import BlockedError, Job, browser, looks_blocked, relative_to_iso

BASE = 'https://www.naukri.com'
API = re.compile(r'/jobapi/v\d+/search')


def _slug(text):
    return re.sub(r'[^a-z0-9]+', '-', (text or '').strip().lower()).strip('-')


def search_url(keywords, location='', page=1):
    slug = '{}-jobs'.format(_slug(keywords))
    if location:
        slug += '-in-{}'.format(_slug(location))
    if page > 1:
        slug += '-{}'.format(page)
    return '{}/{}'.format(BASE, slug)


class Naukri:
    def __init__(self, headless=True, timeout=45000, delay=1.5):
        self.headless = headless
        self.timeout = timeout
        self.delay = delay

    def search(self, keywords, location='', limit=20, posted_within_days=None):
        # accepted for a uniform signature; Naukri's age filter lives behind the
        # same API we cannot call, so utils.jobsearch filters this one locally
        del posted_within_days
        jobs = []
        seen = set()

        with browser(headless=self.headless, timeout=self.timeout) as page:
            captured = []
            page.on('response', lambda response: self._capture(response, captured))

            page_number = 1
            while len(jobs) < limit and page_number <= 15:
                before = len(captured)
                page.goto(search_url(keywords, location, page_number),
                          wait_until='domcontentloaded')
                if looks_blocked(page):
                    raise BlockedError(
                        'Naukri served a bot check. Retry later, or run with --show '
                        'to solve it in a visible window.')

                try:
                    page.wait_for_selector('.srp-jobtuple-wrapper', timeout=15000)
                except Exception:
                    break
                page.wait_for_timeout(int(self.delay * 1000))

                found = self._from_api(captured[before:]) or self._from_dom(page)
                if not found:
                    break

                for job in found:
                    if job.id in seen:
                        continue
                    seen.add(job.id)
                    jobs.append(job)
                    if len(jobs) >= limit:
                        break

                page_number += 1

        return jobs[:limit]

    def _capture(self, response, captured):
        if not API.search(response.url) or response.status != 200:
            return
        try:
            captured.append(response.json())
        except Exception:
            return

    # -- the site's own API payload ---------------------------------------

    def _from_api(self, payloads):
        jobs = []
        for payload in payloads:
            for item in (payload or {}).get('jobDetails') or []:
                job = self._api_job(item)
                if job is not None:
                    jobs.append(job)
        return jobs

    def _api_job(self, item):
        title = item.get('title')
        if not title:
            return None

        fields = {}
        for placeholder in item.get('placeholders') or []:
            if placeholder.get('type') and placeholder.get('label'):
                fields[placeholder['type']] = placeholder['label']

        url = item.get('jdURL') or ''
        if url.startswith('/'):
            url = BASE + url

        posted_text = item.get('footerPlaceholderLabel') or item.get('createdDate')
        return Job(
            source='naukri',
            id=str(item.get('jobId')) if item.get('jobId') else None,
            title=title.strip(),
            company=item.get('companyName'),
            location=fields.get('location'),
            url=url or None,
            posted=relative_to_iso(posted_text),
            posted_text=posted_text,
            salary=self._salary(fields.get('salary')),
            experience_text=fields.get('experience'),
        )

    # -- rendered job tuples ----------------------------------------------

    def _from_dom(self, page):
        cards = page.locator('.srp-jobtuple-wrapper')
        jobs = []

        for index in range(cards.count()):
            card = cards.nth(index)
            try:
                title = card.locator('a.title').first
                if not title.count():
                    continue
                url = title.get_attribute('href') or ''
                posted_text = self._text(card, '.job-post-day')

                jobs.append(Job(
                    source='naukri',
                    id=card.get_attribute('data-job-id') or self._id_from_url(url),
                    title=(title.inner_text() or '').strip(),
                    company=self._text(card, '.comp-name'),
                    location=self._text(card, '.locWdth'),
                    url=url.split('?')[0] or None,
                    posted=relative_to_iso(posted_text),
                    posted_text=posted_text,
                    salary=self._salary(self._text(card, '.sal')),
                    experience_text=self._text(card, '.expwdth'),
                ))
            except Exception:
                continue

        return jobs

    def _text(self, card, selector):
        node = card.locator(selector).first
        if not node.count():
            return None
        try:
            return (node.inner_text() or '').strip() or None
        except Exception:
            return None

    def _id_from_url(self, url):
        match = re.search(r'-(\d{6,})(?:\?|$)', url or '')
        return match.group(1) if match else None

    def _salary(self, value):
        if not value or 'not disclosed' in value.lower():
            return None
        return value
