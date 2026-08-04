"""Indeed job search.

Indeed renders its results server side but also ships them as JSON in a
`window.mosaic.providerData["mosaic-provider-jobcards"]` blob, which is far
steadier than its markup. Plain HTTP usually works; when Indeed decides to
challenge the request we retry the same parse through a real browser.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlencode

import httpx

from ..browser import USER_AGENT, browser, looks_blocked
from ..dates import epoch_to_iso
from ..models import BlockedError, Job
from . import CAPABILITIES

BASE = 'https://www.indeed.com'
PAGE_SIZE = 10  # what Indeed advances `start` by, even though a page holds more

# Indeed runs a site per country and the US one quietly ignores `l=India`,
# answering with US jobs instead - so the country picks the host, not the query
COUNTRY_HOSTS = {
    'india': 'in',
    'united kingdom': 'uk',
    'uk': 'uk',
    'england': 'uk',
    'canada': 'ca',
    'australia': 'au',
    'germany': 'de',
    'france': 'fr',
    'singapore': 'sg',
    'ireland': 'ie',
    'netherlands': 'nl',
    'spain': 'es',
    'italy': 'it',
    'new zealand': 'nz',
    'south africa': 'za',
    'japan': 'jp',
    'brazil': 'br',
    'mexico': 'mx',
    'poland': 'pl',
    'sweden': 'se',
    'united arab emirates': 'ae',
    'uae': 'ae',
    'switzerland': 'ch',
}


def host_for(location):
    """'Bengaluru, India' -> 'https://in.indeed.com'. Defaults to the US site."""
    text = (location or '').strip().lower()
    for country, code in COUNTRY_HOSTS.items():
        if re.search(r'\b{}\b'.format(re.escape(country)), text):
            return 'https://{}.indeed.com'.format(code)
    return BASE


HEADERS = {
    'User-Agent': USER_AGENT,
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

MOSAIC = re.compile(
    r'window\.mosaic\.providerData\[\s*["\']mosaic-provider-jobcards["\']\s*\]\s*=\s*(\{.+?\})\s*;',
    re.DOTALL,
)


class Indeed:
    capability = CAPABILITIES['indeed']

    def __init__(self, domain=None, delay=1.0, timeout=30.0, headless=True, client=None):
        # None means "work it out from the search location"
        self.domain = domain.rstrip('/') if domain else None
        self.host = self.domain or BASE  # re-resolved per search from the location
        self.delay = delay
        self.headless = headless
        self.client = client or httpx.Client(
            headers=HEADERS, timeout=timeout, follow_redirects=True
        )

    def search(self, keywords, location='', limit=25, posted_within_days=None):
        self.host = self.domain or host_for(location)
        jobs = []
        seen = set()
        start = 0

        while len(jobs) < limit:
            url = self._url(keywords, location, start, posted_within_days)
            html = self._html(url)
            results = self._results(html)
            if not results:
                break

            before = len(jobs)
            for item in results:
                job = self._to_job(item)
                if job.id in seen:
                    continue
                seen.add(job.id)
                jobs.append(job)
                if len(jobs) >= limit:
                    break

            # Indeed clamps `start` past a few hundred results and re-serves the
            # same page. Without this the loop never ends.
            if len(jobs) == before:
                break

            start += PAGE_SIZE
            if len(jobs) < limit:
                time.sleep(self.delay)

        return jobs[:limit]

    def close(self):
        """Release the HTTP client. Safe to call more than once."""
        self.client.close()

    def _url(self, keywords, location, start, posted_within_days=None):
        query = {'q': keywords, 'l': location}
        if start:
            query['start'] = start
        if posted_within_days:
            query['fromage'] = int(posted_within_days)
        return '{}/jobs?{}'.format(self.host, urlencode(query))

    def _html(self, url):
        try:
            response = self.client.get(url)
            if response.status_code == 200 and MOSAIC.search(response.text):
                return response.text
        except httpx.TransportError:
            pass
        # Indeed challenged or reshaped the plain request - try it in a browser
        return self._html_via_browser(url)

    def _html_via_browser(self, url):
        with browser(headless=self.headless) as page:
            page.goto(url, wait_until='domcontentloaded')
            if looks_blocked(page):
                raise BlockedError(
                    'Indeed served a bot check instead of results. Retry later, or run '
                    'with --show to solve it in a visible window.'
                )
            return page.content()

    def _results(self, html):
        match = MOSAIC.search(html)
        if not match:
            raise BlockedError('no job cards found in the Indeed response')
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            raise BlockedError('Indeed job card payload was not valid JSON') from None
        model = (payload.get('metaData') or {}).get('mosaicProviderJobCardsModel') or {}
        return model.get('results') or []

    def _to_job(self, item):
        link = item.get('link') or ''
        if link.startswith('/'):
            link = self.host + link
        salary = (item.get('salarySnippet') or {}).get('text')

        return Job(
            source='indeed',
            id=item.get('jobkey'),
            title=item.get('title'),
            company=item.get('company'),
            location=item.get('formattedLocation'),
            url=link or None,
            posted=epoch_to_iso(item.get('pubDate')),
            posted_text=item.get('formattedRelativeTime'),
            employment_type=self._employment_type(item),
            salary=salary or None,
            remote=bool(item.get('remoteLocation')) or None,
        )

    def _employment_type(self, item):
        for attribute in item.get('jobTypes') or []:
            if isinstance(attribute, str):
                return attribute
        for attribute in item.get('taxonomyAttributes') or []:
            if attribute.get('label') in ('job-types', 'jobTypes'):
                values = attribute.get('attributes') or []
                if values:
                    return values[0].get('label')
        return None
