from __future__ import annotations

import json
import re
import time

import httpx

from .errors import CompanyNotFound, ParseError, ReviewsError
from .models import CompanyRating, Review

BASE = 'https://www.ambitionbox.com'
PAGE_SIZE = 20
# AmbitionBox stops paginating at 500 pages, so 10k reviews is the hard ceiling
MAX_PAGES = 500

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
LD_JSON = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)

# ratingsData keys -> the names we expose, minus overallCompanyRating which is the headline
BREAKDOWN = {
    'workLifeRating': 'work_life_balance',
    'skillDevelopmentRating': 'skill_development',
    'compensationBenefitsRating': 'salary_and_benefits',
    'jobSecurityRating': 'job_security',
    'careerGrowthRating': 'career_growth',
    'workSatisfactionRating': 'work_satisfaction',
    'companyCultureRating': 'company_culture',
}


def slugify(company):
    """'Tata Consultancy' -> 'tata-consultancy'. Already-slugged input passes through."""
    slug = re.sub(r'[^a-z0-9]+', '-', company.strip().lower()).strip('-')
    # the url already ends in -reviews, so drop a trailing one the caller may have pasted
    return re.sub(r'-reviews$', '', slug)


class AmbitionBoxClient:
    """Reads company ratings straight out of AmbitionBox's server-rendered Next.js data.

    No browser needed: the reviews page ships every field we want inside its
    __NEXT_DATA__ blob, and page 2+ are available as plain JSON from Next's own
    data route.
    """

    def __init__(self, timeout=30.0, delay=1.0, retries=3, client=None):
        self.delay = delay
        self.retries = retries
        self.client = client or httpx.Client(
            headers=HEADERS, timeout=timeout, follow_redirects=True
        )

    def _get(self, url, **kwargs) -> httpx.Response:
        """GET with backoff, so one flaky hop does not kill a multi-page scrape."""
        last_error: Exception | None = None
        response: httpx.Response | None = None
        for attempt in range(max(1, self.retries)):
            if attempt:
                time.sleep(self.delay * 2**attempt)
            try:
                response = self.client.get(url, **kwargs)
            except httpx.TransportError as error:
                last_error = error
                continue
            if response.status_code in (429, 502, 503):
                last_error = None
                continue
            return response
        if response is not None:
            # exhausted the retries on 429/502/503; the caller raises on the status
            return response
        if last_error is not None:
            raise last_error
        raise ReviewsError('no response from {}'.format(url))

    def fetch(self, company, max_reviews=PAGE_SIZE):
        slug = slugify(company)
        url = '{}/reviews/{}-reviews'.format(BASE, slug)

        response = self._get(url)
        if response.status_code == 404:
            raise CompanyNotFound('no AmbitionBox page for {!r} (tried {})'.format(company, url))
        response.raise_for_status()

        try:
            props = self._page_props(response.text)
        except ParseError:
            # shape changed on us - the JSON-LD aggregate still carries rating + count
            return self._from_json_ld(response.text, company, url)

        rating = self._to_rating(props, company, url)

        wanted = max(0, max_reviews)
        rating.reviews = [self._to_review(item) for item in props.get('reviewsData') or []][:wanted]

        total_pages = min((props.get('pagination') or {}).get('totalPages') or 1, MAX_PAGES)
        build_id = props.get('__buildId')
        page = 2
        while len(rating.reviews) < wanted and page <= total_pages and build_id:
            time.sleep(self.delay)
            page_props, build_id = self._fetch_page(slug, build_id, page)
            items = page_props.get('reviewsData') or []
            if not items:
                break
            for item in items:
                if len(rating.reviews) >= wanted:
                    break
                rating.reviews.append(self._to_review(item))
            page += 1

        return rating

    # -- fetching ---------------------------------------------------------

    def _fetch_page(self, slug, build_id, page):
        """Page 2+ via Next's data route. Retries once if the build id has rotated."""
        response = self._data_route(slug, build_id, page)
        if response.status_code == 404:
            build_id = self._refresh_build_id(slug)
            response = self._data_route(slug, build_id, page)
        response.raise_for_status()
        return response.json().get('pageProps') or {}, build_id

    def _data_route(self, slug, build_id, page):
        return self._get(
            '{}/_next/data/{}/reviews/{}-reviews.json'.format(BASE, build_id, slug),
            params={'page': page, 'companyName': '{}-reviews'.format(slug)},
            headers={'x-nextjs-data': '1'},
        )

    def _refresh_build_id(self, slug):
        response = self._get('{}/reviews/{}-reviews'.format(BASE, slug))
        response.raise_for_status()
        return self._page_props(response.text).get('__buildId')

    # -- parsing ----------------------------------------------------------

    def _page_props(self, html):
        match = NEXT_DATA.search(html)
        if not match:
            raise ParseError('__NEXT_DATA__ script not found')
        try:
            payload = json.loads(match.group(1))
        except ValueError as error:
            raise ParseError('__NEXT_DATA__ is not valid JSON: {}'.format(error)) from error

        props = (payload.get('props') or {}).get('pageProps')
        if not props:
            raise ParseError('__NEXT_DATA__ has no props.pageProps')
        # stash the build id here so callers get it alongside the data it belongs to
        props['__buildId'] = payload.get('buildId')
        return props

    def _to_rating(self, props, company, url):
        ratings = props.get('ratingsData') or {}
        header = props.get('companyHeaderData') or {}
        count = (
            props.get('reviewCount') or props.get('fixedReviewCount') or header.get('reviewsCount')
        )

        distribution = {}
        for bucket in props.get('ratingDistribution') or []:
            if bucket.get('rating') is not None:
                distribution[int(bucket['rating'])] = bucket.get('count')

        company_id = props.get('companyId') or header.get('companyId')
        return CompanyRating(
            source='ambitionbox',
            company=props.get('companyName') or header.get('companyName') or company,
            url=url,
            company_id=str(company_id) if company_id is not None else None,
            overall_rating=ratings.get('overallCompanyRating') or header.get('rating'),
            review_count=count,
            rating_breakdown={
                name: ratings[key] for key, name in BREAKDOWN.items() if key in ratings
            },
            rating_distribution=distribution,
        )

    def _to_review(self, item):
        return Review(
            rating=item.get('overallCompanyRating'),
            title=item.get('reviewTitle'),
            pros=item.get('likesText'),
            cons=item.get('disLikesText'),
            date=item.get('modifiedMachineReadable') or item.get('created'),
            job_title=(item.get('jobProfile') or {}).get('name'),
            location=(item.get('jobLocation') or {}).get('name'),
            source='ambitionbox',
        )

    def _from_json_ld(self, html, company, url):
        """Last resort: the EmployerAggregateRating block still gives rating + count."""
        for match in LD_JSON.finditer(html):
            try:
                block = json.loads(match.group(1))
            except ValueError:
                continue
            if block.get('@type') != 'EmployerAggregateRating':
                continue
            reviewed = block.get('itemReviewed') or {}
            return CompanyRating(
                source='ambitionbox',
                company=reviewed.get('name') or company,
                url=url,
                overall_rating=float(block['ratingValue']) if block.get('ratingValue') else None,
                review_count=block.get('ratingCount'),
            )
        raise ParseError(
            'neither __NEXT_DATA__ nor EmployerAggregateRating found on {}'.format(url)
        )
