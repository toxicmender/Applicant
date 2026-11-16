"""What is stored right now: jobs found, and what came of them."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

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


@dataclass(frozen=True)
class Status:
    input: str
    log: str
    by_source: dict[str, int]
    by_status: dict[str, int]
    projection_pending: bool = False

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
        }


def summarise(input: str, log: str, backend: Backend = 'files') -> Status:
    by_source: dict[str, int] = {}
    from .application_attempts import AttemptJournal

    pending = AttemptJournal.drain_registered(log, backend)
    if pending:
        jobs, counts = _committed(input, log, backend)
        logger.warning(
            'status: application projection deferred; showing the last committed summary'
        )
    else:
        jobs = listing(input, backend).load()
        counts = applications(log, backend).counts()
    for job in jobs:
        by_source[job.source] = by_source.get(job.source, 0) + 1
    status = Status(input, log, by_source, counts, pending)
    logger.info(f'status: {status.jobs} job(s) in {input}, log {log}')
    return status


def _committed(input, log, backend):
    from ..infra.store.sqlite import DB_NAME, Store

    paths = [Path(input).resolve(), Path(log).resolve()]
    jobs = listing(input, 'files').load()
    counts = applications(log, 'files').counts()
    if backend == 'sqlite':
        for kind, path in zip(('jobs', 'applications'), paths, strict=True):
            database = path.parent / DB_NAME
            if not database.is_file():
                continue
            with Store.open_readonly(database) as store, store.read_snapshot():
                name = store._managed_name(kind, path)
                if kind == 'jobs':
                    payloads = store.db.execute('SELECT payload FROM jobs WHERE listing=?', (name,))
                    jobs = jobs_from((json.loads(row[0]) for row in payloads), input)
                else:
                    counts = dict(
                        store.db.execute(
                            "SELECT COALESCE(NULLIF(status, ''), 'unknown'), COUNT(*) "
                            'FROM applications WHERE log=? GROUP BY 1',
                            (name,),
                        )
                    )
    return jobs, counts
