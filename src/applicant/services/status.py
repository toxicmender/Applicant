"""What is stored right now: jobs found, and what came of them."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..infra.store.repositories import Backend, applications, listing

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Status:
    input: str
    log: str
    by_source: dict[str, int]
    by_status: dict[str, int]

    @property
    def jobs(self) -> int:
        return sum(self.by_source.values())

    @property
    def applications(self) -> int:
        return sum(self.by_status.values())

    def to_dict(self) -> dict:
        """The machine readable form `status --json` writes."""
        return {
            'input': self.input,
            'log': self.log,
            'jobs': {'total': self.jobs, 'by_source': self.by_source},
            'applications': {'total': self.applications, 'by_status': self.by_status},
        }


def summarise(input: str, log: str, backend: Backend = 'files') -> Status:
    by_source: dict[str, int] = {}
    for job in listing(input, backend).load():
        by_source[job.source] = by_source.get(job.source, 0) + 1
    status = Status(input, log, by_source, applications(log, backend).counts())
    logger.info(f'status: {status.jobs} job(s) in {input}, log {log}')
    return status
