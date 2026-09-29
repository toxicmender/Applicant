"""One interface over the two ways of keeping jobs and applications.

`files` is the JSON listing and the CSV log alone - how the tool worked before
the database, and still the default for library callers, so importing
applicant never creates a database behind anyone's back. `sqlite` keeps
applicant.db beside the files as the record and exports them after every
change; the CLI uses it unless told otherwise. Both give the same answers:
the application log's dedupe is one planner (`storage.plan_rows`) either way.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, Protocol

from ...domain.job import Job
from ...storage import ApplicationLog, Applied, jobs_from, load_jobs, save_jobs
from .sqlite import DB_NAME, Store

Backend = Literal['files', 'sqlite']
Entry = tuple[Job, str, str]


class Listing(Protocol):
    path: str

    def load(self) -> list[Job]: ...

    def save(self, jobs: Iterable[Job]) -> int: ...


class Applications(Protocol):
    path: str

    def record(self, entries: Iterable[Entry]) -> int: ...

    def rows(self) -> list[dict]: ...

    def counts(self) -> dict[str, int]: ...

    def applied(self) -> Applied: ...


class FileListing:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> list[Job]:
        return load_jobs(self.path)

    def save(self, jobs: Iterable[Job]) -> int:
        return save_jobs(jobs, self.path)


def _nothing_there(path: str) -> bool:
    """Neither the file nor a database beside it: a read has nothing to find,
    and must not leave an empty applicant.db behind for having looked."""
    return not os.path.exists(path) and not (Path(path).resolve().parent / DB_NAME).exists()


class StoreListing:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> list[Job]:
        if _nothing_there(self.path):
            return []
        with Store.beside(self.path) as store:
            return jobs_from(store.job_items(self.path), self.path)

    def save(self, jobs: Iterable[Job]) -> int:
        with Store.beside(self.path) as store:
            return store.save_jobs(jobs, self.path)


class StoreApplications:
    def __init__(self, path: str):
        self.path = path

    def record(self, entries: Iterable[Entry]) -> int:
        with Store.beside(self.path) as store:
            return store.record_applications(entries, self.path)

    def rows(self) -> list[dict]:
        if _nothing_there(self.path):
            return []
        with Store.beside(self.path) as store:
            return store.application_rows(self.path)

    def applied(self) -> Applied:
        return Applied.from_rows(self.rows())

    def counts(self) -> dict[str, int]:
        if _nothing_there(self.path):
            return {}
        with Store.beside(self.path) as store:
            return store.application_counts(self.path)


def listing(path: str | os.PathLike[str], backend: Backend = 'files') -> Listing:
    path = os.fspath(path)
    return StoreListing(path) if backend == 'sqlite' else FileListing(path)


def applications(path: str | os.PathLike[str], backend: Backend = 'files') -> Applications:
    path = os.fspath(path)
    return StoreApplications(path) if backend == 'sqlite' else ApplicationLog(path)
