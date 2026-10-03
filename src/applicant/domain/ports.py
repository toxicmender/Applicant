"""What the core asks of the outside world, as Protocols.

Only ports the code actually depends on are here. The rest of the plan's list
(job sources, repositories) are structural already - `services.search.Board`,
`infra.store.repositories.Listing` - and move here if a second caller needs them.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

from .job import Job

# what an application came to, as the log records it - part of the file
# format of applied_jobs.csv, so defined here once
STATUSES = ('applied', 'needs_manual_apply', 'would_apply', 'failed')
# rows that record an attempt rather than an outcome: a dry run, or a failure.
# They stay in the log as history, but do not stop a later run's outcome for
# the same posting from being recorded
PROVISIONAL = frozenset({'would_apply', 'failed'})

# The notes an Easy Apply outcome is logged with. Two different reasons end in
# needs_manual_apply, and the person reading the log has to be able to tell
# them apart: in one case nothing was sent, in the other something may have been.
NOT_EASY_APPLY = 'not easy apply, or a multi step form'
UNCONFIRMED = (
    'submitted, but LinkedIn did not confirm it - check your LinkedIn applications '
    'before applying again'
)


@dataclass(frozen=True)
class ApplicationResult:
    job: Job
    status: str  # one of STATUSES
    note: str

    def entry(self) -> tuple[Job, str, str]:
        """The (job, status, note) row the application log takes."""
        return self.job, self.status, self.note


def easy_apply_results(
    jobs: list[Job],
    applied: Collection[str],
    unconfirmed: Collection[str],
    failed: Mapping[str, str] | None = None,
    *,
    tried_only: bool = False,
) -> list[ApplicationResult]:
    """What an Easy Apply run came to, one result per job, from the urls it
    applied to, the ones it submitted without LinkedIn confirming them, and
    the ones that failed before anything was sent (url -> why).

    `tried_only`: leave out the jobs that got neither outcome - after an
    interrupt they were never tried, and with no row a later run offers them.
    """
    results = []
    for job in jobs:
        if job.url in applied:
            results.append(ApplicationResult(job, 'applied', 'linkedin easy apply'))
        elif job.url in unconfirmed:
            results.append(ApplicationResult(job, 'needs_manual_apply', UNCONFIRMED))
        elif failed and job.url is not None and job.url in failed:
            # provisional: a later run tries it again
            results.append(ApplicationResult(job, 'failed', failed[job.url]))
        elif not tried_only:
            results.append(ApplicationResult(job, 'needs_manual_apply', NOT_EASY_APPLY))
    return results


class Interrupted(KeyboardInterrupt):
    """Ctrl-C part way through applying, carrying what had already happened.

    Applications sent before the interrupt are real, so they must still reach
    the log. Being a KeyboardInterrupt, it still stops the run and exits 130.
    """

    def __init__(self, results: list[ApplicationResult]):
        super().__init__()
        self.results = results


class Applier(Protocol):
    """Something that can apply to a board's postings on the user's behalf.

    `dry_run` is keyword-only with no default, on purpose: a call that submits
    applications in someone's name must say so. A dry run touches nothing.
    """

    source: str

    def apply(self, jobs: list[Job], *, dry_run: bool) -> list[ApplicationResult]: ...
