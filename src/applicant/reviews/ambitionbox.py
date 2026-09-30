from __future__ import annotations

import json
import logging
import re

import httpx

from ..errors import NotFound, SourceError, Unparseable
from ..infra.http import HEADERS, HttpClient
from .models import CompanyRating, Review

logger = logging.getLogger(__name__)

BASE = 'https://www.ambitionbox.com'
PAGE_SIZE = 20
# AmbitionBox stops paginating at 500 pages, so 10k reviews is the hard ceiling
MAX_PAGES = 500

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
        # `delay` is both the gap between pages and the base of the retry
        # backoff, as it always was; HttpClient now does both
        self.http = HttpClient(
            'AmbitionBox',
            client=client,
            headers=HEADERS,
            timeout=timeout,
            retries=retries,
            backoff=delay,
            interval=delay,
        )
        self.client = self.http.client

    def _get(self, url, **kwargs) -> httpx.Response:
        return self.http.get(url, **kwargs)

    def fetch(self, company, max_reviews=PAGE_SIZE):
        """Every failure leaves as a SourceError, so a caller trying several
        sources can report this one and carry on with the rest. The network
        failing through every retry arrives as `Unreachable`, a rate limit that
        outlasts them as `Blocked` - both SourceErrors."""
        try:
            rating = self._fetch(company, max_reviews)
        except httpx.HTTPStatusError as error:
            raise SourceError(
                f'AmbitionBox answered HTTP {error.response.status_code} for {company!r}'
            ) from error
        except httpx.HTTPError as error:
            raise SourceError(f'could not reach AmbitionBox: {error}') from error
        except ValueError as error:  # a JSON body that did not parse
            raise Unparseable(f'AmbitionBox sent unreadable data: {error}') from error
        logger.info(
            f'ambitionbox: {rating.company}: {rating.overall_rating} from '
            f'{rating.review_count} ratings, {len(rating.reviews)} review(s)'
        )
        return rating

    def _fetch(self, company, max_reviews):
        slug = slugify(company)
        url = '{}/reviews/{}-reviews'.format(BASE, slug)

        response = self._get(url)
        if response.status_code == 404:
            raise NotFound('no AmbitionBox page for {!r} (tried {})'.format(company, url))
        response.raise_for_status()

        try:
            props = self._page_props(response.text)
        except Unparseable as error:
            # shape changed on us - the JSON-LD aggregate still carries rating + count
            logger.warning(f'ambitionbox: {error}; falling back to the JSON-LD summary')
            return self._from_json_ld(response.text, company, url)

        rating = self._to_rating(props, company, url)

        wanted = max(0, max_reviews)
        rating.reviews = [self._to_review(item) for item in props.get('reviewsData') or []][:wanted]

        total_pages = min((props.get('pagination') or {}).get('totalPages') or 1, MAX_PAGES)
        build_id = props.get('__buildId')
        page = 2
        while len(rating.reviews) < wanted and page <= total_pages and build_id:
            try:
                page_props, build_id = self._fetch_page(slug, build_id, page)
            # a later page failing costs only the reviews still to come: the
            # rating and the reviews already read are kept
            except (SourceError, httpx.HTTPError, ValueError) as error:
                logger.warning(
                    f'ambitionbox: review page {page} failed ({type(error).__name__}: '
                    f'{error}); keeping the {len(rating.reviews)} review(s) already read'
                )
                break
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
            raise Unparseable('__NEXT_DATA__ script not found')
        try:
            payload = json.loads(match.group(1))
        except ValueError as error:
            raise Unparseable('__NEXT_DATA__ is not valid JSON: {}'.format(error)) from error

        props = (payload.get('props') or {}).get('pageProps')
        if not props:
            raise Unparseable('__NEXT_DATA__ has no props.pageProps')
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
        raise Unparseable(
            'neither __NEXT_DATA__ nor EmployerAggregateRating found on {}'.format(url)
        )
