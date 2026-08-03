from __future__ import annotations

import json
import re

from .errors import ChallengeError, ParseError, ReviewsError
from .models import CompanyRating, Review

BASE = 'https://www.glassdoor.com'
PAGE_SIZE = 10

# https://www.glassdoor.com/Reviews/Google-Reviews-E9079.htm
REVIEWS_URL = re.compile(r'^https?://[^/]*glassdoor\.[^/]+/Reviews/(?P<slug>[^/]+?)-Reviews-E(?P<id>\d+)',
                         re.IGNORECASE)
SLUG_AND_ID = re.compile(r'^(?P<slug>.+?)-?E(?P<id>\d+)$', re.IGNORECASE)
APOLLO_STATE = re.compile(r'apolloState"\s*:\s*(\{.+?\})\s*\}\s*;', re.DOTALL)
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')


def reviews_url(company, page=1):
    """Accepts a full reviews url or a 'Google-E9079' style slug+id pair."""
    match = REVIEWS_URL.match(company) or SLUG_AND_ID.match(company)
    if not match:
        raise ReviewsError(
            'Glassdoor needs a reviews url or a slug with employer id (e.g. "Google-E9079"), '
            'got {!r}. Glassdoor company search is behind the same bot check, so names '
            'cannot be resolved automatically.'.format(company))
    url = '{}/Reviews/{}-Reviews-E{}.htm'.format(BASE, match.group('slug'), match.group('id'))
    if page > 1:
        url = url.replace('.htm', '_P{}.htm'.format(page))
    return url, match.group('slug'), match.group('id')


class GlassdoorClient:
    """Drives a real Chromium profile and reads Glassdoor's own review payloads.

    Glassdoor sits behind Cloudflare, so the first run is deliberately headed:
    you clear the challenge (and sign in, if you want more than the first page)
    once, and the persistent profile is reused headlessly afterwards.

    Reviews are taken from the GraphQL/XHR responses the page fetches for itself,
    falling back to the apolloState blob embedded in the HTML.
    """

    def __init__(self, profile_dir='.gd_profile', login=False, headless=True, timeout=45000):
        self.profile_dir = profile_dir
        self.login = login
        self.headless = False if login else headless
        self.timeout = timeout

    def fetch(self, company, max_reviews=PAGE_SIZE):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ReviewsError('the glassdoor source needs playwright: '
                               'pip install playwright && playwright install chromium')

        url, slug, employer_id = reviews_url(company)
        payloads = []
        html_pages = []

        with sync_playwright() as driver:
            context = driver.chromium.launch_persistent_context(
                self.profile_dir,
                headless=self.headless,
                user_agent=USER_AGENT,
                locale='en-US',
                viewport={'width': 1366, 'height': 900},
                args=['--disable-blink-features=AutomationControlled', '--disable-extensions'],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(self.timeout)
                page.on('response', lambda response: self._capture(response, payloads))

                page.goto(url, wait_until='domcontentloaded')
                self._settle(page)
                html_pages.append(page.content())

                # Glassdoor shows ~10 reviews per page; keep paging until we have enough
                current = 1
                while self._review_count(payloads, html_pages) < max_reviews and current < 30:
                    current += 1
                    next_url, _, _ = reviews_url(company, current)
                    response = page.goto(next_url, wait_until='domcontentloaded')
                    if response is not None and response.status >= 400:
                        break
                    self._settle(page)
                    html_pages.append(page.content())

                if self.login:
                    self._save_state(context)
            finally:
                context.close()

        rating = self._build(payloads, html_pages, slug, employer_id, url)
        rating.reviews = rating.reviews[:max(0, max_reviews)]
        return rating

    # -- browser ----------------------------------------------------------

    def _settle(self, page):
        """Let XHRs land, and refuse to guess when a bot check is in the way."""
        try:
            page.wait_for_load_state('networkidle', timeout=self.timeout)
        except Exception:
            pass  # networkidle never arrives on some pages; the captured payloads still count

        if self.login:
            print('A browser window is open. Clear the Cloudflare check and sign in to '
                  'Glassdoor, then come back here.')
            input('Press Enter once the reviews page is visible: ')
            try:
                page.wait_for_load_state('networkidle', timeout=self.timeout)
            except Exception:
                pass
            return

        if self._blocked(page):
            raise ChallengeError(
                'Glassdoor served a bot check instead of the reviews page. Re-run with '
                '--login to clear it once in a visible browser; the saved profile is '
                'reused headlessly afterwards.')

    def _blocked(self, page):
        try:
            title = page.title()
        except Exception:
            title = ''
        if 'just a moment' in title.lower() or 'attention required' in title.lower():
            return True
        return page.locator('#challenge-platform, #cf-challenge-running').count() > 0

    def _capture(self, response, payloads):
        if '/graph' not in response.url:
            return
        try:
            body = response.json()
        except Exception:
            return  # non-json, redirect, or a body that is already gone
        if body:
            payloads.append(body)

    def _save_state(self, context):
        try:
            context.storage_state(path='storage_state.json')
        except Exception as error:
            print('could not write storage_state.json: {}'.format(error))

    # -- parsing ----------------------------------------------------------

    def _build(self, payloads, html_pages, slug, employer_id, url):
        sources = list(payloads)
        for html in html_pages:
            sources.extend(self._embedded(html))

        if not sources:
            raise ParseError('no review data found in Glassdoor responses for {}'.format(url))

        rating = CompanyRating(
            source='glassdoor',
            company=slug.replace('-', ' '),
            url=url,
            company_id=employer_id,
        )

        seen = set()
        for source in sources:
            for node in self._walk(source):
                self._read_aggregate(node, rating)
                review = self._read_review(node)
                if review is None:
                    continue
                key = (review.pros, review.cons, review.title)
                if key not in seen:
                    seen.add(key)
                    rating.reviews.append(review)

        return rating

    def _embedded(self, html):
        """apolloState is the reliable in-page cache; __NEXT_DATA__ is there on some routes."""
        found = []
        for match in APOLLO_STATE.finditer(html):
            try:
                found.append(json.loads(match.group(1)))
            except ValueError:
                continue
        match = NEXT_DATA.search(html)
        if match:
            try:
                found.append(json.loads(match.group(1)))
            except ValueError:
                pass
        return found

    def _walk(self, node):
        """Yield every dict in the tree.

        Apollo cache keys move between deploys, so we match on field names rather
        than on a fixed path.
        """
        stack = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                yield current
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)

    def _read_aggregate(self, node, rating):
        if rating.overall_rating is None:
            value = node.get('ratingOverall') if isinstance(node.get('ratingOverall'), (int, float)) else None
            if value:
                rating.overall_rating = float(value)
        if rating.review_count is None:
            for key in ('reviewCount', 'allReviewsCount', 'totalReviewCount', 'ratingCount'):
                value = node.get(key)
                if isinstance(value, int) and value > 0:
                    rating.review_count = value
                    break
        for key, name in (('ratingWorkLifeBalance', 'work_life_balance'),
                          ('ratingCultureAndValues', 'culture_and_values'),
                          ('ratingCareerOpportunities', 'career_opportunities'),
                          ('ratingCompensationAndBenefits', 'salary_and_benefits'),
                          ('ratingSeniorLeadership', 'senior_leadership'),
                          ('ratingDiversityAndInclusion', 'diversity_and_inclusion')):
            value = node.get(key)
            if isinstance(value, (int, float)) and name not in rating.rating_breakdown:
                rating.rating_breakdown[name] = float(value)

    def _read_review(self, node):
        if 'pros' not in node and 'cons' not in node:
            return None
        pros, cons = node.get('pros'), node.get('cons')
        if not isinstance(pros, str) and not isinstance(cons, str):
            return None

        job_title = node.get('jobTitle')
        if isinstance(job_title, dict):
            job_title = job_title.get('text')
        location = node.get('location')
        if isinstance(location, dict):
            location = location.get('name')

        rating = node.get('ratingOverall')
        date = node.get('reviewDateTime') or node.get('reviewDate')
        return Review(
            rating=float(rating) if isinstance(rating, (int, float)) else None,
            title=node.get('summary') or node.get('reviewTitle'),
            pros=pros if isinstance(pros, str) else None,
            cons=cons if isinstance(cons, str) else None,
            date=date[:10] if isinstance(date, str) else None,
            job_title=job_title if isinstance(job_title, str) else None,
            location=location if isinstance(location, str) else None,
            source='glassdoor',
        )

    def _review_count(self, payloads, html_pages):
        """Cheap progress check for the pagination loop."""
        total = 0
        for source in list(payloads) + [page for html in html_pages for page in self._embedded(html)]:
            for node in self._walk(source):
                if self._read_review(node) is not None:
                    total += 1
        return total
