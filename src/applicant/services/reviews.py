"""Company ratings from every review site asked, one failure costing only itself."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..files import write_document
from ..infra.store.repositories import Backend
from .events import Emit, RatingFetched, ignore
from .fanout import fan_out

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
) -> Reviews:
    """Ask each client for `company`'s rating and write whatever came back.

    One rating is written as an object, several as a list - the shape the file
    has always had: this run's ratings. With the sqlite backend every rating is
    also kept in applicant.db, so the file is today's view and the database
    the history (it used to be overwritten, and the history lost).
    """

    def one(source: str) -> Any:
        logger.info(f'reviews: fetching {company!r} from {source}')
        rating = clients[source].fetch(company, max_reviews=max_reviews)
        emit(RatingFetched(source, rating))
        return rating

    outcomes = fan_out(clients, one, emit)
    ratings = [outcome.result for outcome in outcomes if outcome.result is not None]
    if not ratings:
        logger.error(f'reviews: no source returned a rating for {company!r}')
        return Reviews([], None)

    stored = [rating.to_dict() for rating in ratings]
    if backend == 'sqlite':
        from ..infra.store.sqlite import Store

        with Store.beside(output) as store:
            store.add_ratings(company, [(rating.source, rating.to_dict()) for rating in ratings])
    write_document(output, stored if len(stored) > 1 else stored[0])
    logger.info(f'reviews: {len(stored)} rating(s) written to {output}')
    return Reviews(ratings, output)
