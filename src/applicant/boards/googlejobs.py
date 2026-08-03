"""Google Jobs - the aggregator built into Google Search.

The old `ibp=htl;jobs` entry point now redirects to the `udm=8` Jobs tab, and
the old `li.iFjolb` card selectors went with it. Google's class names are
obfuscated and rotate, so cards are found structurally and their visible text
is classified line by line rather than read out of fixed elements.

This is Google Search, so it needs a real browser and it is the most fragile
source here by some distance.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlencode

from ..browser import browser, looks_blocked
from ..dates import relative_to_iso
from ..models import BlockedError, Job

BASE = 'https://www.google.com/search'
CARD = 'div[jsname="y1Aese"][role="button"]'

VIA = re.compile(r'^(?P<location>.*?)\s*[•·]\s*via\s+(?P<via>.+)$', re.IGNORECASE)
POSTED = re.compile(r'\b(ago|just posted|today|yesterday)\b', re.IGNORECASE)
# en dashes and non breaking spaces are everywhere in this UI
TYPES = re.compile(
    r'^(full|part)[\s‐-―-]*time$|^(contractor|contract|internship|intern|temporary|volunteer)$',
    re.IGNORECASE,
)
SALARY = re.compile(
    r'[₹$€£¥]|\ba (?:month|year|week|day)\b|\ban hour\b|\bper hour\b|\bK\b', re.IGNORECASE
)


class GoogleJobs:
    def __init__(self, headless=True, timeout=45000, hl='en'):
        self.headless = headless
        self.timeout = timeout
        self.hl = hl

    def search(self, keywords, location='', limit=20, posted_within_days=None):
        # accepted for a uniform signature; Google's date chip is not
        # addressable by url, so applicant.filters is applied locally instead
        del posted_within_days
        query = '{} jobs'.format(keywords)
        if location:
            query += ' in {}'.format(location)

        url = '{}?{}'.format(BASE, urlencode({'q': query, 'udm': '8', 'hl': self.hl}))

        with browser(headless=self.headless, timeout=self.timeout) as page:
            page.goto(url, wait_until='domcontentloaded')
            if looks_blocked(page):
                raise BlockedError(
                    'Google served a bot check instead of job results. Google Search is '
                    'strict about automation - retry later or from another network.'
                )

            try:
                page.wait_for_selector(CARD, timeout=20000)
            except Exception:  # noqa: BLE001 - wait_for_selector times out with its own error type
                raise BlockedError(
                    'no job cards on the Google Jobs page. The Jobs tab may be '
                    'unavailable for this query or region, or the layout changed again.'
                ) from None

            self._load_more(page, limit)

            jobs = []
            seen = set()
            cards = page.locator(CARD)
            for index in range(cards.count()):
                try:
                    text = cards.nth(index).inner_text()
                except Exception:  # noqa: BLE001 - a card detached mid-scroll
                    continue
                job = self._to_job(text)
                if job is None:
                    continue
                key = (job.title, job.company, job.location)
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(job)
                if len(jobs) >= limit:
                    break

        return jobs[:limit]

    def close(self):
        """Nothing is held between searches; the browser closes with each one."""

    def _load_more(self, page, limit):
        """The list lazy loads in blocks of ten as the window scrolls.

        The first scroll usually adds nothing, so a single stall is not the end
        of the list - only give up after a few in a row.
        """
        cards = page.locator(CARD)
        stalls = 0
        for _ in range(30):
            count = cards.count()
            if count >= limit:
                break
            page.mouse.wheel(0, 6000)
            page.wait_for_timeout(1500)
            if cards.count() == count:
                stalls += 1
                if stalls >= 3:
                    break
            else:
                stalls = 0

    def _to_job(self, text):
        lines = [line.strip() for line in (text or '').split('\n') if line.strip()]
        # a bare initial is the company logo placeholder
        while lines and len(lines[0]) <= 1:
            lines.pop(0)
        if len(lines) < 2:
            return None

        title, company = lines[0], lines[1]
        location = via = posted_text = salary = employment_type = None

        for line in lines[2:]:
            match = VIA.match(line)
            if match:
                location = match.group('location').strip() or None
                via = match.group('via').strip()
                continue
            if employment_type is None and TYPES.match(line):
                employment_type = line
                continue
            if posted_text is None and POSTED.search(line):
                posted_text = line
                continue
            if salary is None and SALARY.search(line):
                salary = line

        # Google exposes no posting id, so derive a stable one from the fields
        # that identify it - without this every card dedupes to the same key
        fingerprint = '|'.join(filter(None, (title, company, location, via)))
        return Job(
            source='googlejobs',
            id=hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16],
            title=title,
            company=company,
            location=location,
            posted=relative_to_iso(posted_text),
            posted_text=posted_text,
            employment_type=employment_type,
            salary=salary,
            via=via,
            # Google routes applications back to the originating board, and the
            # link only exists once a card is opened
            url=None,
        )
