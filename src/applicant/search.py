"""One interface over the job boards: search, filter, apply, record.

    from applicant.search import Jobs
    from applicant.filters import JobFilter

    with Jobs() as board:
        hits = board.search('python developer', JobFilter(location='India', posted_within_days=7))
        board.apply(hits, log='applied_jobs.csv')

`Jobs` is the front door, and deliberately thin: it knows how to make each
board, and keeps the LinkedIn session that Easy Apply needs alive between a
search and an apply. The work itself is in `applicant.services.search` and
`applicant.services.apply`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING

from .errors import SourceError
from .filters import JobFilter
from .infra.store.repositories import Backend
from .log import get
from .models import Job
from .services.apply import ApplyToJobs, Entry, easy_apply_with
from .services.events import Emit, Event, SourceFailed, ignore
from .services.search import SOURCES, Board, SearchJobs, close_quietly

if TYPE_CHECKING:
    from .boards.linkedin import LinkedIn

__all__ = ['EASY_APPLY_LISTING', 'SOURCES', 'Board', 'Jobs']

logger = get(__name__)

# Deprecated and unused: Easy Apply used to stage jobs in this file for the
# LinkedIn client to read back. They are handed over directly now; the name is
# kept only so code that patched it does not break.
EASY_APPLY_LISTING = 'applied_via_jobs_interface.json'


class Jobs:
    """The facade: one search across boards, one filter, one apply, one log."""

    def __init__(
        self,
        sources: Sequence[str] = SOURCES,
        headless: bool = True,
        linkedin: LinkedIn | None = None,
        backend: Backend = 'files',
    ):
        self.sources = tuple(sources)
        self.headless = headless
        self._linkedin = linkedin
        # where apply() logs: 'files' is the CSV alone, 'sqlite' the database
        # beside it with the CSV exported (see applicant.infra.store)
        self.backend: Backend = backend

    def linkedin(self) -> LinkedIn:
        """The LinkedIn client, which outlives a single search because its
        browser session is what `easy_apply` needs."""
        if self._linkedin is None:
            from .boards.linkedin import LinkedIn

            self._linkedin = LinkedIn(headless=self.headless)
        return self._linkedin

    def close(self) -> None:
        """Release the LinkedIn session this facade kept open for `apply`.

        Every other board is closed as soon as its search is done. Safe to call
        more than once, and a failure is logged rather than raised.
        """
        if self._linkedin is not None:
            close_quietly('linkedin', self._linkedin)
            self._linkedin = None

    def __enter__(self) -> Jobs:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _client(self, name: str) -> Board:
        if name == 'linkedin':
            return self.linkedin()
        if name == 'indeed':
            from .boards.indeed import Indeed

            return Indeed(headless=self.headless)
        if name == 'naukri':
            from .boards.naukri import Naukri

            return Naukri(headless=self.headless)
        if name == 'googlejobs':
            from .boards.googlejobs import GoogleJobs

            return GoogleJobs(headless=self.headless)
        raise SourceError('unknown source {!r}'.format(name))

    def search(
        self,
        keywords: str,
        filters: JobFilter | None = None,
        limit: int = 25,
        want: int | None = None,
        max_rounds: int = 4,
        enrich: bool = False,
        enrich_limit: int = 25,
        on_error: Callable[[str, Exception], None] | None = None,
        emit: Emit = ignore,
    ) -> list[Job]:
        """Search every configured board and return the filtered, merged results.

        See `SearchJobs.run` for `limit`, `want`, `max_rounds` and `enrich`.
        `on_error(board, error)` hears about each board that failed; `emit`
        receives every event as it happens.
        """
        if on_error is not None:
            emit = _with_on_error(emit, on_error)
        return SearchJobs(self._client, keep_open={'linkedin'}).run(
            self.sources,
            keywords,
            filters or JobFilter(),
            limit=limit,
            want=want,
            max_rounds=max_rounds,
            enrich=enrich,
            enrich_limit=enrich_limit,
            emit=emit,
        )

    def apply(
        self,
        jobs: Iterable[Job],
        log: str = 'applied_jobs.csv',
        filters: JobFilter | None = None,
        dry_run: bool = False,
    ) -> list[Entry]:
        """Apply where it is actually possible, and record everything.

        Only LinkedIn Easy Apply can be automated; see `ApplyToJobs`.
        """
        return ApplyToJobs(self._easy_apply, self.backend).run(
            jobs, log=log, filters=filters, dry_run=dry_run
        )

    def _easy_apply(self, jobs: list[Job]) -> list[Entry]:
        return easy_apply_with(self.linkedin(), jobs)


def _with_on_error(emit: Emit, on_error: Callable[[str, Exception], None]) -> Emit:
    """The older `on_error` callback, fed from the event stream."""

    def both(event: Event) -> None:
        if isinstance(event, SourceFailed) and isinstance(event.error, Exception):
            on_error(event.source, event.error)
        emit(event)

    return both
