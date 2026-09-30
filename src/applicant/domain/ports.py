"""What the core asks of the outside world, as Protocols.

Only ports the code actually depends on are here. The rest of the plan's list
(job sources, repositories) are structural already - `services.search.Board`,
`infra.store.repositories.Listing` - and move here if a second caller needs them.
"""

from __future__ import annotations

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


@dataclass(frozen=True)
class ApplicationResult:
    job: Job
    status: str  # one of STATUSES
    note: str

    def entry(self) -> tuple[Job, str, str]:
        """The (job, status, note) row the application log takes."""
        return self.job, self.status, self.note


class Applier(Protocol):
    """Something that can apply to a board's postings on the user's behalf.

    `dry_run` is keyword-only with no default, on purpose: a call that submits
    applications in someone's name must say so. A dry run touches nothing.
    """

    source: str

    def apply(self, jobs: list[Job], *, dry_run: bool) -> list[ApplicationResult]: ...
