"""`JobFilter` as the rest of the package uses it: wired to the real world.

The filtering itself is pure and lives in `applicant.domain.filtering`. This
module supplies the two things the domain deliberately does not reach for:

* the rate table - the shared, disk-cached `applicant.money.Rates` unless one
  is given, which may fetch on a miss
* which board publishes what - `applicant.boards.capability`

and `prepared()`, which fetches every rate a batch of jobs needs once, up
front, so the filtering that follows is pure and every job in the run is
compared at the same rate.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

from .boards import capability
from .domain.filtering import (
    Check,
    ExperienceCheck,
    FilterContext,
    LocationCheck,
    PostedCheck,
    SalaryCheck,
    SilencePolicy,
    TextCheck,
    Verdict,
)
from .domain.filtering import JobFilter as _PureJobFilter
from .domain.job import Job
from .domain.rates import RateSnapshot, RateTable
from .domain.salary import parse_salary

__all__ = [
    'Check',
    'ExperienceCheck',
    'FilterContext',
    'JobFilter',
    'LocationCheck',
    'PostedCheck',
    'SalaryCheck',
    'SilencePolicy',
    'TextCheck',
    'Verdict',
    'prepared',
]


class JobFilter(_PureJobFilter):
    """The domain `JobFilter`, answering from live rates and the board table.

    `rates=None` means the shared cache (`applicant.money.rates()`), which
    fetches on a miss; pass `Rates(offline=True)` or a `RateSnapshot` to keep a
    run off the network.
    """

    def _rate_table(self) -> RateTable:
        if self.rates is not None:
            return self.rates
        from .money import rates

        return rates()

    def _capability_of(self):
        return capability


def _needs_rates(filters: _PureJobFilter) -> bool:
    return (
        filters.min_salary is not None
        and bool(filters.currency)
        and filters.salary_basis != 'strict'
    )


def prepared(filters: _PureJobFilter, jobs: Iterable[Job]) -> _PureJobFilter:
    """`filters` with every rate these jobs need fetched now, as a snapshot.

    Without this, the salary check asks the live cache per job, and a rate the
    network will not give is asked for again on every posting. A filter whose
    table is already a snapshot - or that compares no pay across currencies -
    comes back unchanged.
    """
    if not _needs_rates(filters) or isinstance(filters.rates, RateSnapshot):
        return filters
    table = filters._rate_table()
    snapshot = getattr(table, 'snapshot', None)
    if not callable(snapshot):
        return filters  # a table of the caller's own: trust it as given

    currencies = set(filters.currencies())
    for job in jobs:
        salary = parse_salary(job.salary)
        if salary is not None and salary.currency:
            currencies.add(salary.currency)
    return dataclasses.replace(filters, rates=snapshot(currencies, basis=filters.salary_basis))
