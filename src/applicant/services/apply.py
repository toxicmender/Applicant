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
from ..domain.ports import ApplicationResult, Applier, Interrupted, easy_apply_results
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
    except KeyboardInterrupt as stop:
        # what was already sent is real: it travels with the interrupt to the log
        done = easy_apply_results(
            jobs, _urls(client, 'applied'), _urls(client, 'unconfirmed'), tried_only=True
        )
        raise Interrupted(done) from stop
    except SourceError as error:
        logger.warning('linkedin: {}'.format(error))
        return [(job, 'failed', str(error)) for job in jobs]
    # e.g. the browser would not start
    except Exception as error:
        logger.error(f'linkedin: easy apply failed: {type(error).__name__}: {error}')
        logger.debug('linkedin: easy apply traceback', exc_info=True)
        return [(job, 'failed', type(error).__name__) for job in jobs]

    # submitted without LinkedIn confirming it: possibly sent, and the note has
    # to say so, or the row reads as "nothing was sent" and invites a second go
    return [
        result.entry() for result in easy_apply_results(jobs, applied, _urls(client, 'unconfirmed'))
    ]


def _urls(client: object, name: str) -> set[str]:
    """The urls a client kept under `name`, if it keeps any (a LinkedIn does)."""
    found = getattr(client, name, None)
    return set(found) if isinstance(found, (list, tuple, set)) else set()


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

        record = applications(log, self.backend)
        # settled on an earlier run - applied to, or found to need a person:
        # never submitted, or offered, again
        done = record.settled()
        entries: list[Entry] = []
        targets: dict[str, list[Job]] = {}
        already = 0

        for job in dedupe.one_per_job(jobs):
            if job.url and job.source in self.appliers:
                if done.covers(job):
                    already += 1
                    continue
                targets.setdefault(job.source, []).append(job)
            else:
                entries.append(
                    (
                        job,
                        'needs_manual_apply',
                        'apply on {} directly'.format(job.via or job.source),
                    )
                )

        if already:
            logger.info(
                f'apply: {already} job(s) already settled in {log} '
                '(applied, or needing a manual application); skipped'
            )
        waiting = [job for batch in targets.values() for job in batch]
        if waiting and not dry_run and self.confirm is not None and not self.confirm(waiting):
            logger.warning(f'apply: {len(waiting)} application(s) not submitted: not confirmed')
            self.declined = waiting
            targets = {}

        try:
            for source, batch in targets.items():
                entries.extend(_apply_isolated(self.appliers[source], source, batch, dry_run))
        except KeyboardInterrupt as stop:
            # stopped part way: what was already sent, and every row for the
            # other boards, still reach the log; jobs never tried get no row,
            # so the next run offers them. Then the interrupt goes on (exit 130)
            entries.extend(result.entry() for result in getattr(stop, 'results', ()))
            written = record.record(entries)
            logger.warning(f'apply: interrupted; {written} new row(s) saved in {log}')
            raise

        written = record.record(entries)
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
