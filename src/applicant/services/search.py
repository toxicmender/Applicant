"""Searching the job boards: pull, filter, re-read, enrich.

Filters are pushed down to each board where it supports them natively
(location, keywords, date posted) and applied locally for the rest, so results
are consistent no matter which board they came from. `applicant.search.Jobs` is
the front door most callers use; this is the work behind it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from ..domain import flags
from ..domain.capability import Capability, Field
from ..domain.job import Job, experience_from
from ..errors import SourceError
from ..filters import JobFilter, prepared
from ..storage import save_jobs
from .events import BoardSearched, Emit, ignore
from .fanout import fan_out

logger = logging.getLogger(__name__)

SOURCES = ('linkedin', 'indeed', 'naukri', 'googlejobs')


class Board(Protocol):
    """What every board module offers, and all a search needs of one."""

    capability: Capability

    def search(
        self,
        keywords: str,
        location: str = '',
        limit: int = 25,
        posted_within_days: int | None = None,
    ) -> list[Job]: ...

    def close(self) -> None: ...


@dataclass
class Enrichment:
    """Reading postings themselves, and the requests that costs.

    `described` is keyed by posting so a board re-read under `want` does not pay
    for the same page twice; `budget` counts only pages actually fetched.
    """

    budget: int
    described: dict[tuple[str | None, str | None], str | None] = field(default_factory=dict)
    stopped: bool = False


def close_quietly(name: str, client: Board) -> None:
    """Release a board. A failure here is logged, never raised: it would mask
    the search result, or the error, that is already on its way out."""
    try:
        client.close()
    except Exception as error:  # noqa: BLE001 - teardown must not mask the result
        logger.warning(f'{name}: could not close cleanly: {type(error).__name__}: {error}')


@dataclass(frozen=True)
class Stored:
    """Where a search's results went: how many were new, how many there are."""

    path: str
    saved: int
    total: int


def store(found: Iterable[Job], path: str) -> Stored:
    """Merge a search's results into the listing file, newest winning."""
    found = list(found)
    total = save_jobs(found, path)
    logger.info(f'search: {len(found)} job(s) saved, {total} now in {path}')
    return Stored(path, len(found), total)


class SearchJobs:
    """One search across several boards, each failure isolated.

    `client_for` makes (or hands back) a board by name. Boards named in
    `keep_open` are not closed after their search - LinkedIn, whose session
    `apply` goes on to reuse.
    """

    def __init__(self, client_for: Callable[[str], Board], keep_open: Collection[str] = ()):
        self.client_for = client_for
        self.keep_open = frozenset(keep_open)

    def run(
        self,
        sources: Iterable[str],
        keywords: str,
        filters: JobFilter,
        limit: int = 25,
        want: int | None = None,
        max_rounds: int = 4,
        enrich: bool = False,
        enrich_limit: int = 25,
        emit: Emit = ignore,
    ) -> list[Job]:
        """Every configured board's filtered results, merged.

        `limit` is per board *before* filtering, so a strict filter returns
        fewer. `want` asks for a number of *survivors* instead: each board is
        re-read with a larger limit until that many get through, the board runs
        out, or `max_rounds` is reached. Left as None each board is read once.

        `enrich` reads the postings themselves for an experience filter the
        search cards could not answer, at most `enrich_limit` of them.
        """
        plan = Enrichment(budget=enrich_limit) if enrich else None

        def one(name: str) -> list[Job]:
            client = self.client_for(name)
            logger.info(f'{name}: searching {keywords!r} in {filters.location or "anywhere"!r}')
            try:
                kept, seen = self._from_board(
                    client, name, keywords, filters, limit, want, max_rounds, plan
                )
            finally:
                if name not in self.keep_open:
                    close_quietly(name, client)
            logger.info('{}: {} of {} jobs match'.format(name, len(kept), seen))
            emit(BoardSearched(name, len(kept), seen))
            return kept

        collected: list[Job] = []
        for outcome in fan_out(sources, one, emit):
            if outcome.result:
                collected.extend(outcome.result)
        return collected

    def _from_board(
        self,
        client: Board,
        name: str,
        keywords: str,
        filters: JobFilter,
        limit: int,
        want: int | None,
        max_rounds: int,
        plan: Enrichment | None = None,
    ) -> tuple[list[Job], int]:
        """One board's surviving jobs, and how many were read to get them.

        Reading more means asking for a bigger page and re-reading what we
        already saw: the boards page from the top, and Google Jobs only exists
        as a scrolling list, so there is no offset to resume from. That is why
        rounds are capped and why one round remains the default - each extra one
        is a fresh set of requests against a site that is watching for exactly
        that.
        """
        native = client.capability.filters
        # whatever the board filtered for us, we must not filter again -
        # see Capability.filters for why a second pass would be wrong
        skip: list[str] = sorted(native)
        posted = filters.posted_within_days if Field.POSTED in native else None

        # when the postings themselves can answer the experience filter, hold it
        # back until they have been read - a card's silence is not an answer
        deferred = (
            plan is not None
            and filters.experience is not None
            and callable(getattr(client, 'describe', None))
        )
        if deferred:
            skip = [*skip, Field.EXPERIENCE]

        pull = limit
        kept: list[Job] = []
        seen = 0

        for round_number in range(max(1, max_rounds) if want else 1):
            jobs = client.search(
                keywords, filters.location or '', limit=pull, posted_within_days=posted
            )
            seen = len(jobs)

            # every rate this batch needs, fetched once - then filtering is pure
            ready = prepared(filters, jobs)
            kept = []
            for job in jobs:
                keep, found = ready.matches(job, skip=skip)
                if not keep:
                    continue
                job.flags = found
                kept.append(job)

            if deferred and plan is not None:
                self._enrich(client, kept, plan)
                kept = self._recheck_experience(kept, filters)

            if want is None or len(kept) >= want:
                break
            if seen < pull:
                # the board gave us everything it had; asking again is pointless
                break
            if round_number + 1 < max_rounds:
                pull *= 2
                logger.info('{}: {} of {} match, reading {}'.format(name, len(kept), seen, pull))

        return (kept[:want] if want else kept), seen

    def _enrich(self, client: Board, jobs: list[Job], plan: Enrichment) -> None:
        """Read the posting itself where its card never stated the experience.

        Only what a filter actually needs, only for jobs that survived every
        other check, and only up to a budget: this is the slowest path in the
        project and the one most likely to be noticed by a site that would
        rather we were not here. Experience only - a salary read out of free
        prose is as likely to be a relocation allowance as a wage, and nothing
        in this project is ever guessed.
        """
        describe = getattr(client, 'describe')  # noqa: B009 - presence is the check

        for job in jobs:
            if job.experience_min is not None or job.experience_max is not None:
                continue

            key = (job.source, job.id)
            if key not in plan.described:
                if plan.stopped or plan.budget <= 0:
                    continue
                try:
                    plan.described[key] = describe(job)
                except SourceError as error:
                    # one refusal means the next request is worse than useless
                    logger.warning('{}: {} (enrichment stopped)'.format(job.source, error))
                    plan.stopped = True
                    continue
                plan.budget -= 1

            found = experience_from(plan.described.get(key))
            if found is None:
                continue

            phrase, low, high = found
            job.experience_text = phrase
            job.experience_min, job.experience_max = low, high
            job.flags = [*job.flags, flags.EXPERIENCE_ENRICHED]

    def _recheck_experience(self, jobs: list[Job], filters: JobFilter) -> list[Job]:
        """The check held back for enrichment, now that the postings have been read."""
        check = JobFilter(
            experience=filters.experience,
            keep_unknown=filters.keep_unknown,
            keep_unpublished=filters.keep_unpublished,
        )
        kept = []
        for job in jobs:
            keep, found = check.matches(job)
            if keep:
                job.flags = [*job.flags, *found]
                kept.append(job)
        return kept
