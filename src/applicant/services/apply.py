"""Applying: choose the postings, pick one copy of each job, act, record.

Only LinkedIn Easy Apply can be automated. Postings on the other boards hand
off to each employer's own form, so they are recorded as needs-manual-apply
with their url rather than guessed at - the CSV doubles as a worklist.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..domain import dedupe
from ..domain.filtering import JobFilter
from ..domain.job import Job
from ..domain.ports import ApplicationResult, Applier
from ..errors import SourceError
from ..filters import prepared
from ..infra.store.repositories import Backend, applications, listing

logger = logging.getLogger(__name__)

# (job, status, note): one row of applied_jobs.csv
Entry = tuple[Job, str, str]


class EasyApplier(Protocol):
    def easy_apply(self, source: Any = ...) -> list[str]: ...


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


def worklist(path: str, filters: JobFilter, backend: Backend = 'files') -> Worklist:
    """The stored jobs at `path` that pass `filters`."""
    jobs = listing(path, backend).load()
    if not jobs:
        logger.warning(f'apply: no jobs could be read from {path}')
    return Worklist(len(jobs), select(jobs, filters))


def easy_apply_with(client: EasyApplier, jobs: list[Job]) -> list[Entry]:
    """Easy Apply to `jobs` through a LinkedIn client, one entry per job.

    The jobs are handed over directly. (They used to be written to a staging
    file for the client to read back.) Every failure becomes a `failed` row
    rather than an exception: the other boards' rows still have to reach the log.
    """
    try:
        applied = set(client.easy_apply(jobs))
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


class EasyApply:
    """The Applier port over anything with `easy_apply(jobs)` - a LinkedIn client.

    `client_for` is called only when there is something to submit, so a dry
    run never loads the account module or opens a browser.
    """

    source = 'linkedin'

    def __init__(self, client_for: Callable[[], EasyApplier]):
        self.client_for = client_for

    def apply(self, jobs: list[Job], *, dry_run: bool) -> list[ApplicationResult]:
        if dry_run:
            return [ApplicationResult(job, 'would_apply', 'dry run') for job in jobs]
        return [ApplicationResult(*entry) for entry in easy_apply_with(self.client_for(), jobs)]


# asked with the postings about to be submitted; True means go ahead
Confirm = Callable[[list[Job]], bool]


class ApplyToJobs:
    """Apply through whichever board has an `Applier`; list the rest.

    `appliers` maps a board to what can apply there - today only LinkedIn.
    `confirm`, when given, is asked before anything is submitted, and a "no"
    submits nothing: those postings are left out of the log, so a later run
    can still apply to them. The CLI always passes one (or `--yes`); a library
    caller who passes `dry_run=False` without one has asked for it in code.
    """

    def __init__(
        self,
        appliers: Mapping[str, Applier],
        backend: Backend = 'files',
        confirm: Confirm | None = None,
    ):
        self.appliers = appliers
        self.backend: Backend = backend
        self.confirm = confirm
        self.declined: list[Job] = []

    def run(
        self,
        jobs: Iterable[Job],
        log: str = 'applied_jobs.csv',
        filters: JobFilter | None = None,
        *,
        dry_run: bool,
    ) -> list[Entry]:
        """Apply where it is actually possible, and record everything.

        `dry_run` is required: nothing here guesses whether you meant it.
        """
        if filters is not None:
            jobs = select(jobs, filters)

        entries: list[Entry] = []
        targets: dict[str, list[Job]] = {}

        for job in dedupe.one_per_job(jobs):
            if job.url and job.source in self.appliers:
                targets.setdefault(job.source, []).append(job)
            else:
                entries.append(
                    (
                        job,
                        'needs_manual_apply',
                        'apply on {} directly'.format(job.via or job.source),
                    )
                )

        waiting = [job for batch in targets.values() for job in batch]
        if waiting and not dry_run and self.confirm is not None and not self.confirm(waiting):
            logger.warning(f'apply: {len(waiting)} application(s) not submitted: not confirmed')
            self.declined = waiting
            targets = {}

        for source, batch in targets.items():
            entries.extend(_apply_isolated(self.appliers[source], source, batch, dry_run))

        written = applications(log, self.backend).record(entries)
        logger.info(
            '{} new rows in {} ({} already recorded)'.format(written, log, len(entries) - written)
        )
        return entries


def _apply_isolated(applier: Applier, source: str, jobs: list[Job], dry_run: bool) -> list[Entry]:
    """One board's applications; its failure becomes `failed` rows, not an exception."""
    try:
        return [result.entry() for result in applier.apply(jobs, dry_run=dry_run)]
    except SourceError as error:
        logger.warning(f'{source}: {error}')
        return [(job, 'failed', str(error)) for job in jobs]
    except Exception as error:
        logger.error(f'{source}: applying failed: {type(error).__name__}: {error}')
        logger.debug(f'{source}: apply traceback', exc_info=True)
        return [(job, 'failed', type(error).__name__) for job in jobs]
