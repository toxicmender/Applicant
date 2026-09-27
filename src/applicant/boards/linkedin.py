"""LinkedIn's guest job search: read-only, no account, no browser.

`LinkedInGuest` reads the job cards LinkedIn serves to logged out clients, and
the postings behind them. It cannot sign in, and cannot apply to anything.

Everything that acts as you - signing in, the recommended jobs page, Easy
Apply - lives in `applicant.boards.linkedin_apply`, which a search never
imports (a test checks). Reading public job cards and submitting applications
on your real account are different risks, and the code that can do the second
is kept small and on its own.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import urlencode

from ..domain.job import Job
from ..errors import SourceError
from ..infra.http import HEADERS, HttpClient
from . import CAPABILITIES

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


class LinkedInGuest:
    """LinkedIn job search as a logged out visitor sees it."""

    capability = CAPABILITIES['linkedin']

    def __init__(self, headless=True, timeout=45000, delay=1.0, client=None):
        # headless and timeout are for the signed in subclass's browser; the
        # guest pages need none
        self.headless = headless
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
            # an unreachable host or a 429 leaves HttpClient as a SourceError
            response = self.http.get('{}?{}'.format(GUEST_SEARCH, urlencode(query)))
            if response.is_error:
                raise SourceError(f'LinkedIn guest search answered HTTP {response.status_code}')

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

    def close(self):
        """Release the HTTP client. Safe to call more than once."""
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
