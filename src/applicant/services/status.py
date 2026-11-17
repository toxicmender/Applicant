"""What is stored right now: jobs found, and what came of them."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict, overload

from ..domain.job import Job
from ..errors import DatabaseBusy
from ..infra.store.publication import nonblocking_exports
from ..infra.store.repositories import Backend, applications, listing
from ..storage import jobs_from

logger = logging.getLogger(__name__)


class JobCounts(TypedDict):
    total: int
    by_source: dict[str, int]


class ApplicationCounts(TypedDict):
    total: int
    by_status: dict[str, int]


class StatusReport(TypedDict):
    input: str
    log: str
    jobs: JobCounts
    applications: ApplicationCounts
    projection_pending: bool
    export_pending: bool


@dataclass(frozen=True)
class Status:
    input: str
    log: str
    by_source: dict[str, int]
    by_status: dict[str, int]
    projection_pending: bool = False
    export_pending: bool = False

    @property
    def jobs(self) -> int:
        return sum(self.by_source.values())

    @property
    def applications(self) -> int:
        return sum(self.by_status.values())

    def to_dict(self) -> StatusReport:
        """The machine readable form `status --json` writes."""
        return {
            'input': self.input,
            'log': self.log,
            'jobs': {'total': self.jobs, 'by_source': self.by_source},
            'applications': {'total': self.applications, 'by_status': self.by_status},
            'projection_pending': self.projection_pending,
            'export_pending': self.export_pending,
        }


def summarise(input: str, log: str, backend: Backend = 'files') -> Status:
    by_source: dict[str, int] = {}
    from .application_attempts import AttemptJournal

    with nonblocking_exports():
        pending = AttemptJournal.drain_registered(log, backend)
        export_pending = False
        try:
            jobs = listing(input, backend).load()
        except DatabaseBusy:
            jobs, _ = _committed(input, backend, 'jobs')
            export_pending = True
        try:
            if pending:
                counts, log_pending = _committed(log, backend, 'applications')
                export_pending |= log_pending
            else:
                counts = applications(log, backend).counts()
        except DatabaseBusy:
            counts, _ = _committed(log, backend, 'applications')
            export_pending = True
    if pending:
        logger.warning(
            'status: application projection deferred; showing the last committed summary'
        )
    if export_pending:
        logger.warning('status: export publication deferred; showing committed data')
    for job in jobs:
        by_source[job.source] = by_source.get(job.source, 0) + 1
    status = Status(input, log, by_source, counts, pending, export_pending)
    logger.info(f'status: {status.jobs} job(s) in {input}, log {log}')
    return status


@overload
def _committed(path, backend, kind: Literal['jobs']) -> tuple[list[Job], bool]: ...


@overload
def _committed(path, backend, kind: Literal['applications']) -> tuple[dict[str, int], bool]: ...


def _committed(path, backend, kind):
    from ..infra.store.sqlite import DB_NAME, Store

    path = Path(path).resolve()
    database = path.parent / DB_NAME
    if backend == 'sqlite' and database.is_file():
        with Store.open_readonly(database) as store, store.read_snapshot():
            name = store._managed_name(kind, path)
            pending = store.db.execute(
                'SELECT 1 FROM pending_exports WHERE kind=? AND document=?', (kind, name)
            ).fetchone()
            if kind == 'jobs':
                items = store.listing_items_snapshot(path)
                if items or pending or store._known_digest(name) is not None:
                    return jobs_from(items, str(path)), bool(pending)
                return listing(path, 'files').load(), False
            counts = dict(
                store.db.execute(
                    "SELECT COALESCE(NULLIF(status, ''), 'unknown'), COUNT(*) "
                    'FROM applications WHERE log=? GROUP BY 1',
                    (name,),
                )
            )
            if pending or counts or store._application_snapshot(name) is not None:
                return counts, bool(pending)
    if kind == 'jobs':
        return listing(path, 'files').load(), False
    return applications(path, 'files').counts(), False
