"""Applying: choose the postings, pick one copy of each job, act, record.

Only LinkedIn Easy Apply can be automated. Postings on the other boards hand
off to each employer's own form, so they are recorded as needs-manual-apply
with their url rather than guessed at - the CSV doubles as a worklist.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol

from ..domain import dedupe
from ..domain.filtering import JobFilter
from ..domain.job import Job
from ..errors import SourceError
from ..filters import prepared
from ..storage import ApplicationLog, load_jobs, save_jobs

logger = logging.getLogger(__name__)

# (job, status, note): one row of applied_jobs.csv
Entry = tuple[Job, str, str]


class EasyApplier(Protocol):
    def easy_apply(self, filepath: str = ...) -> list[str]: ...


def select(jobs: Iterable[Job], filters: JobFilter) -> list[Job]:
    """The jobs that pass `filters`, each carrying the flags of this check.

    Flags are replaced rather than added to, so the log says why a posting
    could not be fully checked now - not what was true at search time.
    """
    jobs = list(jobs)
    ready = prepared(filters, jobs)
    kept = []
    for job in jobs:
        keep, found = ready.matches(job)
        if keep:
            job.flags = found
            kept.append(job)
    return kept


@dataclass(frozen=True)
class Worklist:
    """What `apply` read, and which of it matched the filter."""

    read: int
    matching: list[Job]


def worklist(path: str, filters: JobFilter) -> Worklist:
    """The stored jobs at `path` that pass `filters`."""
    jobs = load_jobs(path)
    if not jobs:
        logger.warning(f'apply: no jobs could be read from {path}')
    return Worklist(len(jobs), select(jobs, filters))


def easy_apply_with(client: EasyApplier, jobs: list[Job], staging: str) -> list[Entry]:
    """Easy Apply to `jobs` through a LinkedIn client, one entry per job.

    Every failure becomes a `failed` row rather than an exception: the other
    boards' rows still have to reach the log.
    """
    save_jobs(jobs, staging)
    try:
        applied = set(client.easy_apply(staging))
    except SourceError as error:
        logger.warning('linkedin: {}'.format(error))
        return [(job, 'failed', str(error)) for job in jobs]
    # e.g. the browser would not start
    except Exception as error:
        logger.error(f'linkedin: easy apply failed: {type(error).__name__}: {error}')
        logger.debug('linkedin: easy apply traceback', exc_info=True)
        return [(job, 'failed', type(error).__name__) for job in jobs]

    return [
        (job, 'applied', 'linkedin easy apply')
        if job.url in applied
        else (job, 'needs_manual_apply', 'not easy apply, or a multi step form')
        for job in jobs
    ]


class ApplyToJobs:
    """`easy_apply` is how LinkedIn postings are applied to; the rest are listed."""

    def __init__(self, easy_apply: Callable[[list[Job]], list[Entry]]):
        self.easy_apply = easy_apply

    def run(
        self,
        jobs: Iterable[Job],
        log: str = 'applied_jobs.csv',
        filters: JobFilter | None = None,
        dry_run: bool = False,
    ) -> list[Entry]:
        """Apply where it is actually possible, and record everything."""
        if filters is not None:
            jobs = select(jobs, filters)

        entries: list[Entry] = []
        linkedin_targets = []

        for job in dedupe.one_per_job(jobs):
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
            entries.extend(self.easy_apply(linkedin_targets))
        elif linkedin_targets:
            entries.extend((job, 'would_apply', 'dry run') for job in linkedin_targets)

        written = ApplicationLog(log).record(entries)
        logger.info(
            '{} new rows in {} ({} already recorded)'.format(written, log, len(entries) - written)
        )
        return entries
