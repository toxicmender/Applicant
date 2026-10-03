"""Tracking company funding across sources and runs."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..financials import FinancialsTracker
from ..infra.store.repositories import Backend, listing
from .events import Emit, FinancialsTracked, ignore
from .fanout import failure, fan_out

logger = logging.getLogger(__name__)

FINANCIAL_SOURCES = ('crunchbase', 'tracxn')


class FinancialsSource(Protocol):
    def fetch(self, company: str, max_rounds: int = ...) -> Any: ...


def companies_from_jobs(path: str, backend: Backend = 'files') -> list[str]:
    """Every distinct company in a job listing, first spelling wins."""
    seen: dict[str, str] = {}
    for job in listing(path, backend).load():
        name = (job.company or '').strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


def companies_to_track(
    named: Iterable[str], from_jobs: str | None = None, backend: Backend = 'files'
) -> list[str]:
    """The companies named, then any others in a job listing, each once."""
    companies = list(named)
    # the same company in another case is the same company - fetched once
    seen = {name.strip().lower() for name in companies}
    if from_jobs:
        for name in companies_from_jobs(from_jobs, backend):
            if name.lower() not in seen:
                seen.add(name.lower())
                companies.append(name)
    return companies


def accepts(client: Any, company: str) -> bool:
    """Whether a source can look `company` up. A profile url belongs to its own
    site; the other one cannot use it."""
    check = getattr(client, 'accepts', None)
    return bool(check(company)) if callable(check) else True


@dataclass(frozen=True)
class Tracked:
    companies: int
    found: int  # (company, source) pairs that came back
    output: str


def track_financials(
    companies: list[str],
    clients: Mapping[str, FinancialsSource],
    output: str,
    max_rounds: int = 20,
    pause: float = 1.0,
    emit: Emit = ignore,
    backend: Backend = 'files',
) -> Tracked:
    """Fetch each company from each source that accepts it, and record what moved.

    `pause` separates companies: both sources rate limit, and the API ones bill
    per call. (Requests within one source are also paced by its HTTP client;
    this covers the browser mode, which is not.)
    """
    logger.info(f'financials: tracking {len(companies)} company(ies) in {output}')
    tracker = FinancialsTracker(output, backend=backend)
    found = 0
    outcomes = []

    for index, company in enumerate(companies):
        if index:
            time.sleep(pause)

        def one(source: str, company: str = company) -> Any:
            logger.info(f'financials: fetching {company!r} from {source}')
            financials = clients[source].fetch(company, max_rounds=max_rounds)
            changes = tracker.record(financials)
            logger.info(f'financials: {source} {company!r}: {len(changes)} change(s) recorded')
            emit(FinancialsTracked(source, financials, changes))
            return financials

        wanted = [source for source, client in clients.items() if accepts(client, company)]
        asked = fan_out(wanted, one, emit, subject=company)
        outcomes.extend(asked)
        found += sum(outcome.ok for outcome in asked)

    stop = failure(outcomes)
    if stop is not None:
        raise stop
    if not found:
        logger.error(f'financials: nothing fetched for {len(companies)} company(ies)')
    return Tracked(len(companies), found, output)
