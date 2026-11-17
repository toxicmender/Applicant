"""Company ratings from every review site asked, one failure costing only itself."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..domain.contracts import now
from ..errors import Unparseable
from ..files import write_document
from ..infra.company_worker import CompanyFetchInterrupted
from ..infra.store.financials_lock import export_lock
from ..infra.store.repositories import Backend
from ..reviews.ambitionbox import AmbitionBoxClient, slugify
from ..reviews.glassdoor import GlassdoorClient, reviews_url
from ..reviews.models import CompanyRating, Review
from .company_cache import CompanyCache, browser_scope, newer_retrieval
from .events import Emit, RatingFetched, ignore
from .fanout import failure, fan_out

logger = logging.getLogger(__name__)

REVIEW_SOURCES = ('ambitionbox', 'glassdoor')


class ReviewSource(Protocol):
    def fetch(self, company: str, max_reviews: int = ...) -> Any: ...


@dataclass(frozen=True)
class Reviews:
    """The ratings that came back, and where they were written (None if none)."""

    ratings: list[Any]
    written_to: str | None


def fetch_reviews(
    company: str,
    clients: Mapping[str, ReviewSource],
    output: str,
    max_reviews: int = 20,
    emit: Emit = ignore,
    backend: Backend = 'files',
    refresh: bool = False,
) -> Reviews:
    """Ask each client for `company`'s rating and write whatever came back.

    One rating is written as an object, several as a list - the shape the file
    has always had: this run's ratings. With the sqlite backend every rating is
    also kept in applicant.db, so the file is today's view and the database
    the history (it used to be overwritten, and the history lost).
    """

    discard = Path(output).resolve() == Path(os.devnull).resolve()
    cache = None if discard else CompanyCache(output)
    completed: list[Any] = []
    observations: list[tuple[str, dict]] = []
    cache_records = {}
    if backend == 'sqlite' and not discard:
        from ..infra.store.sqlite import Store

        with Store.managed(output, 'ratings') as (store, target):
            store._reconcile_export('ratings', store._managed_name('ratings', target), target)

    def one(source: str) -> Any:
        client = clients[source]
        scope, locator = None, company
        if isinstance(client, AmbitionBoxClient):
            scope, locator = 'public', slugify(company)
        elif isinstance(client, GlassdoorClient):
            scope, locator = browser_scope(client.profile_dir), reviews_url(company)[2]
        previous = (
            cache.entry(source, locator, scope) if cache is not None and scope is not None else None
        )
        if (
            cache is not None
            and previous
            and not refresh
            and previous['coverage'] >= max_reviews
            and cache.clock.fresh(previous['retrieved_at'], 7)
        ):
            payload = previous['payload'].copy()
            matched = CompanyRating(**payload)
            matched.reviews = [Review(**row) for row in payload['reviews'][: max(0, max_reviews)]]
            cache_records[len(completed)] = (source, locator, scope, previous, None, None)
            completed.append(matched)
            emit(RatingFetched(source, matched, cached_at=previous['retrieved_at']))
            return matched
        logger.info(f'reviews: fetching {company!r} from {source}')
        rating = client.fetch(company, max_reviews=max_reviews)
        stamp = (cache.clock.current() if cache is not None else now()).isoformat()
        if isinstance(rating, CompanyRating) and (
            rating.overall_rating is None and rating.review_count is None and not rating.reviews
        ):
            raise Unparseable('provider did not return usable review data')
        if previous and (not rating.identity_verified or rating.company_id != previous['identity']):
            raise Unparseable('review refresh did not verify the cached company')
        payload = rating.to_dict()
        if (
            cache is not None
            and scope is not None
            and isinstance(rating, CompanyRating)
            and rating.identity_verified
            and (
                not isinstance(client, GlassdoorClient)
                or browser_scope(client.profile_dir) == scope
            )
        ):
            cache_records[len(completed)] = (
                source,
                locator,
                scope,
                previous,
                rating,
                stamp,
            )
        completed.append(rating)
        observations.append((stamp, payload))
        emit(RatingFetched(source, rating))
        return rating

    def publish(write):
        payloads = [rating.to_dict() for rating in completed]
        if cache is None:
            write(payloads)
            return
        updates = []
        for index, (source, locator, scope, previous, rating, stamp) in cache_records.items():
            current = cache.entry(source, locator, scope)
            if current is not None and (
                current != previous
                or rating is None
                or current['coverage'] > len(rating.reviews)
                or not newer_retrieval(current, stamp)
            ):
                payloads[index] = {
                    **current['payload'],
                    'reviews': current['payload']['reviews'][: max(0, max_reviews)],
                }
            elif rating is not None:
                updates.append((source, locator, scope, previous, rating, stamp))
        write(payloads)
        for source, locator, scope, previous, rating, stamp in updates:
            cache.keep_locked(
                source,
                locator,
                scope,
                rating.company_id,
                previous,
                len(rating.reviews),
                rating.to_dict(),
                stamp,
            )

    def persist():
        if discard:
            return
        with cache.locked() if cache is not None else nullcontext():
            persist_locked()

    def persist_locked():
        if backend == 'sqlite':
            from ..infra.store.sqlite import Store

            with Store.managed(output, 'ratings') as (store, target):
                publish(
                    lambda payloads: store._publish_ratings(
                        target,
                        company,
                        payloads,
                        observations,
                    )
                )
        else:
            with export_lock(output):
                publish(
                    lambda payloads: write_document(
                        output,
                        payloads if len(payloads) > 1 else payloads[0],
                    )
                )

    try:
        outcomes = fan_out(clients, one, emit)
    except CompanyFetchInterrupted as interrupt:
        completed.append(interrupt.result)
        observations.append((now().isoformat(), interrupt.result.to_dict()))
        persist()
        raise
    except KeyboardInterrupt:
        if completed:
            persist()
        raise
    stop = failure(outcomes)
    if stop is not None:
        raise stop
    ratings = [outcome.result for outcome in outcomes if outcome.result is not None]
    if not ratings:
        logger.error(f'reviews: no source returned a rating for {company!r}')
        return Reviews([], None)

    persist()
    logger.info(f'reviews: {len(ratings)} rating(s) written to {output}')
    return Reviews(ratings, output)
