"""Tracking company funding across sources and runs."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import ApplicantError, Unparseable
from ..financials import FinancialsTracker
from ..financials.crunchbase import CrunchbaseClient, permalink
from ..financials.models import CompanyFinancials
from ..financials.tracxn import FinancialsInterrupted
from ..infra.company_worker import CompanyFetchInterrupted
from ..infra.interrupts import defer_interrupt
from ..infra.store.repositories import Backend, listing
from .company_cache import CompanyCache, browser_scope
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
        # companies_from_jobs already gives each company once
        for name in companies_from_jobs(from_jobs, backend):
            if name.lower() not in seen:
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
    refresh: bool = False,
) -> Tracked:
    """Fetch each company from each source that accepts it, and record what moved.

    `pause` separates companies: both sources rate limit, and the API ones bill
    per call. (Requests within one source are also paced by its HTTP client;
    this covers the browser mode, which is not.)
    """
    logger.info(f'financials: tracking {len(companies)} company(ies) in {output}')
    tracker = FinancialsTracker(output, backend=backend)
    cache = CompanyCache(output)
    found = 0
    outcomes = []

    for index, company in enumerate(companies):
        if index:
            time.sleep(pause)

        def one(source: str, company: str = company) -> Any:
            client = clients[source]
            requires_lookup = getattr(client, 'requires_lookup', None)
            use_history = (
                source == 'tracxn'
                and callable(requires_lookup)
                and requires_lookup(company) is True
            )
            if (
                use_history
                and not refresh
                and (cached := tracker.cached_tracxn(company, fresh=cache.clock.fresh))
            ):
                financials, checked = cached
                financials.rounds = financials.rounds[: max(0, max_rounds)]
                emit(FinancialsTracked(source, financials, cached_at=checked))
                return financials
            scope = None
            previous = None
            if isinstance(client, CrunchbaseClient) and not client.api_key and '://' not in company:
                scope = browser_scope(client.profile_dir)
            locator = permalink(company)
            if scope is not None:
                previous = cache.entry(source, locator, scope)
                if (
                    previous
                    and not refresh
                    and previous['coverage'] >= max_rounds
                    and cache.clock.fresh(previous['retrieved_at'], 30)
                ):
                    saved = CompanyFinancials.model_validate(previous['payload'])
                    saved.rounds = saved.rounds[: max(0, max_rounds)]
                    emit(FinancialsTracked(source, saved, cached_at=previous['retrieved_at']))
                    return saved
            logger.info(f'financials: fetching {company!r} from {source}')
            try:
                financials = client.fetch(company, max_rounds=max_rounds)
            except (FinancialsInterrupted, CompanyFetchInterrupted) as interrupt:
                captured = (
                    interrupt.financials
                    if isinstance(interrupt, FinancialsInterrupted)
                    else interrupt.result
                )
                try:
                    with defer_interrupt():
                        tracker.record(captured, require_complete=use_history)
                except ApplicantError as error:
                    logger.error('could not save the interrupted company capture: %s', error)
                raise
            with defer_interrupt():
                verified = (
                    isinstance(financials, CompanyFinancials) and financials._identity_verified
                )
                if previous and not verified:
                    raise Unparseable('Crunchbase refresh did not verify the cached company')
                changes: list[str] = []

                def save():
                    changes.extend(
                        tracker.record(financials, require_complete=use_history or verified)
                    )

                if (
                    verified
                    and scope is not None
                    and isinstance(client, CrunchbaseClient)
                    and (browser_scope(client.profile_dir) == scope)
                ):
                    coverage = len(financials.rounds)
                    if (
                        financials.funding_rounds_count is not None
                        and coverage >= financials.funding_rounds_count
                    ):
                        coverage = max(coverage, max_rounds)
                    cache.keep(
                        source,
                        locator,
                        scope,
                        financials.company_id,
                        previous,
                        coverage,
                        financials.to_dict(),
                        cache.clock.current().isoformat(),
                        commit=save,
                    )
                else:
                    save()
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
