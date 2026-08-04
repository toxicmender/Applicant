"""One interface over the job boards: search, filter, apply, record.

    from applicant.search import Jobs
    from applicant.filters import JobFilter

    board = Jobs()
    hits = board.search('python developer', JobFilter(location='India', posted_within_days=7))
    board.apply(hits, log='applied_jobs.csv')

Filters are pushed down to each board where it supports them natively (location,
keywords, date posted) and applied locally for the rest, so results are consistent
no matter which board they came from.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TYPE_CHECKING, Protocol

from .boards import Capability
from .filters import JobFilter
from .models import Job, JobsError
from .storage import ApplicationLog, save_jobs

if TYPE_CHECKING:
    from .boards.linkedin import LinkedIn


class Board(Protocol):
    """What every board module offers, and all this facade needs of one."""

    capability: Capability

    def search(
        self,
        keywords: str,
        location: str = '',
        limit: int = 25,
        posted_within_days: int | None = None,
    ) -> list[Job]: ...

    def close(self) -> None: ...


SOURCES = ('linkedin', 'indeed', 'naukri', 'googlejobs')

# where jobs handed to _easy_apply are staged for the LinkedIn client to read back
EASY_APPLY_LISTING = 'applied_via_jobs_interface.json'


class Jobs:
    """The facade: one search across boards, one filter, one apply, one log."""

    def __init__(
        self,
        sources: Sequence[str] = SOURCES,
        headless: bool = True,
        linkedin: LinkedIn | None = None,
    ):
        self.sources = tuple(sources)
        self.headless = headless
        self._linkedin = linkedin

    def linkedin(self) -> LinkedIn:
        """The LinkedIn client, which outlives a single search because its
        browser session is what `easy_apply` needs."""
        if self._linkedin is None:
            from .boards.linkedin import LinkedIn

            self._linkedin = LinkedIn(headless=self.headless)
        return self._linkedin

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
        raise JobsError('unknown source {!r}'.format(name))

    def search(
        self,
        keywords: str,
        filters: JobFilter | None = None,
        limit: int = 25,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> list[Job]:
        """Search every configured board and return the filtered, merged results.

        `limit` is per board *before* filtering, so a strict filter returns fewer.
        """
        filters = filters or JobFilter()
        collected: list[Job] = []

        for name in self.sources:
            client = self._client(name)
            native = client.capability.filters
            try:
                jobs = client.search(
                    keywords,
                    filters.location or '',
                    limit=limit,
                    posted_within_days=(filters.posted_within_days if 'posted' in native else None),
                )
            except JobsError as error:
                (on_error or self._report)(name, error)
                continue
            finally:
                # LinkedIn keeps its session open: apply() reuses it
                if name != 'linkedin':
                    client.close()

            # whatever the board filtered for us, we must not filter again -
            # see Capability.filters for why a second pass would be wrong
            skip = sorted(native)

            kept = []
            for job in jobs:
                keep, flags = filters.matches(job, skip=skip)
                if not keep:
                    continue
                job.flags = flags
                kept.append(job)

            print('{}: {} of {} jobs match'.format(name, len(kept), len(jobs)))
            collected.extend(kept)

        return collected

    def _report(self, name: str, error: Exception) -> None:
        print('{}: {}'.format(name, error))

    def apply(
        self,
        jobs: Iterable[Job],
        log: str = 'applied_jobs.csv',
        filters: JobFilter | None = None,
        dry_run: bool = False,
    ) -> list[tuple[Job, str, str]]:
        """Apply where it is actually possible, and record everything.

        Only LinkedIn Easy Apply can be automated. Postings on the other boards
        hand off to each employer's own form, so they are recorded as
        needs-manual-apply with their url rather than guessed at.
        """
        if filters is not None:
            kept = []
            for job in jobs:
                keep, flags = filters.matches(job)
                if keep:
                    # record why this one could not be fully checked, so the log
                    # says so rather than carrying stale flags from search time
                    job.flags = flags
                    kept.append(job)
            jobs = kept

        entries: list[tuple[Job, str, str]] = []
        linkedin_targets = []

        for job in jobs:
            if job.source == 'linkedin' and job.url:
                linkedin_targets.append(job)
            else:
                entries.append(
                    (
                        job,
                        'needs_manual_apply',
                        'apply on {} directly'.format(job.via or job.source),
                    )
                )

        if linkedin_targets and not dry_run:
            entries.extend(self._easy_apply(linkedin_targets))
        elif linkedin_targets:
            entries.extend((job, 'would_apply', 'dry run') for job in linkedin_targets)

        written = ApplicationLog(log).record(entries)
        print(
            '{} new rows in {} ({} already recorded)'.format(written, log, len(entries) - written)
        )
        return entries

    def _easy_apply(self, jobs: list[Job]) -> list[tuple[Job, str, str]]:
        client = self.linkedin()
        results = []
        save_jobs(jobs, EASY_APPLY_LISTING)

        try:
            applied = set(client.easy_apply(EASY_APPLY_LISTING))
        except JobsError as error:
            print('linkedin: {}'.format(error))
            return [(job, 'failed', str(error)) for job in jobs]

        for job in jobs:
            if job.url in applied:
                results.append((job, 'applied', 'linkedin easy apply'))
            else:
                results.append((job, 'needs_manual_apply', 'not easy apply, or a multi step form'))
        return results
