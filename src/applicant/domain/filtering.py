"""Deciding which postings survive, and saying what could not be checked.

`JobFilter` is the configuration a person writes: every field optional, unset
fields constraining nothing. It compiles to a list of `Check`s, one per
criterion, each answering with a `Verdict`. `matches()` runs them in order and
stops at the first that drops the job.

A check is pure. What it needs from outside comes in a `FilterContext`: today's
date, a `RateTable` for comparing pay across currencies, and a way to look up
what a job's board publishes. Nothing here fetches a rate or imports a board -
the caller hands those in (`applicant.filters.JobFilter` does, for anyone using
the old import path).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Protocol

from . import flags
from .capability import UNKNOWN, Capability, Field
from .job import Job
from .places import within
from .rates import EMPTY, RateTable, convert
from .salary import parse_salary

logger = logging.getLogger(__name__)

# which board a job came from -> what that board publishes
CapabilityOf = Callable[[str | None], Capability]


def _unknown_board(source: str | None) -> Capability:
    """With no board table to hand, every source publishes everything, so a
    missing field reads as the posting's silence - the strictest reading."""
    del source
    return UNKNOWN


@dataclass(frozen=True)
class Verdict:
    """One check's answer: keep or drop, and a flag if it could not be sure."""

    keep: bool
    flag: str | None = None
    reason: str | None = None  # for the debug "why dropped" line


KEEP = Verdict(True)


@dataclass(frozen=True)
class SilencePolicy:
    """What becomes of a posting a check could not verify.

    `keep_unknown` keeps every unverifiable posting, flagged. Without it,
    `keep_unpublished` still keeps those whose board never publishes the field -
    their silence is the board's, not the posting's.
    """

    keep_unknown: bool = True
    keep_unpublished: bool = False

    def unverifiable(self, publishes: frozenset[Field], field: Field) -> Verdict:
        """Silence has two very different sources. A posting on a board that
        publishes experience and states none is being evasive; a posting on a
        board that never publishes it at all has said nothing wrong. Only the
        first is a reason to drop anything, which is why `--strict` alone -
        drop everything unverifiable - deletes three of the four boards the
        moment an experience filter is set.
        """
        if field not in publishes:
            return Verdict(
                self.keep_unknown or self.keep_unpublished, flags.UNPUBLISHED[field.value]
            )
        return Verdict(self.keep_unknown, flags.UNKNOWN[field.value])


@dataclass(frozen=True)
class FilterContext:
    """Everything a check may consult beyond the job itself."""

    today: date
    rates: RateTable
    policy: SilencePolicy
    capability_of: CapabilityOf = _unknown_board

    def unverifiable(self, job: Job, field: Field) -> Verdict:
        return self.policy.unverifiable(self.capability_of(job.source).publishes, field)


class Check(Protocol):
    """One criterion. `field` names what it examines, so a board that already
    filtered on that field server side can have the check skipped."""

    @property
    def field(self) -> Field: ...

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict: ...


# -- the checks -----------------------------------------------------------


@dataclass(frozen=True)
class TextCheck:
    """Title or company: any one of several phrases, every word of it present.

    Substring matching, deliberately: "ml" should find "AI/ML". The cost is
    that a two letter filter overreaches - "ai" is inside "Trainee" - which is
    why several narrow filters beat one short one.
    """

    field: Field
    wanted: tuple[str, ...]

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict:
        actual = (getattr(job, self.field.value) or '').lower()
        if any(all(word in actual for word in one.lower().split()) for one in self.wanted):
            return KEEP
        return Verdict(False, reason=f'{self.field.value} did not match')


@dataclass(frozen=True)
class LocationCheck:
    """Text first, then containment: 'India' has to accept 'Bengaluru'.

    Only the boards spell a country out; `apply` reads a stored file where the
    location is whatever the board said, so without this a country filter
    empties the worklist instead of narrowing it.
    """

    wanted: str
    field: Field = Field.LOCATION

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict:
        verdict = within(self.wanted, job.location)
        if verdict is None:
            return Verdict(ctx.policy.keep_unknown, flags.LOCATION_UNVERIFIED)
        return Verdict(verdict, reason=None if verdict else 'location outside the one asked for')


@dataclass(frozen=True)
class SalaryCheck:
    """The top of the posted range against an annual floor, across currencies."""

    minimum: float
    currency: str | None
    basis: str
    field: Field = Field.SALARY

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict:
        salary = parse_salary(job.salary)
        top = salary.annual_high if salary is not None else None
        if salary is None or top is None:
            return ctx.unverifiable(job, self.field)

        flag = None
        if self.currency and salary.currency and salary.currency != self.currency:
            if self.basis == 'strict':
                return Verdict(ctx.policy.keep_unknown, flags.SALARY_CURRENCY_MISMATCH)
            top, note = convert(
                top, salary.currency, self.currency, basis=self.basis, table=ctx.rates
            )
            if top is None:
                return Verdict(ctx.policy.keep_unknown, note or flags.SALARY_CURRENCY_MISMATCH)
            flag = note  # e.g. fell back off ppp to market
        elif self.currency and not salary.currency:
            # a bare number with no symbol - assume it is already in `currency`
            flag = flags.SALARY_CURRENCY_ASSUMED

        if top >= self.minimum:
            return Verdict(True, flag)
        return Verdict(False, flag, 'salary under minimum')


@dataclass(frozen=True)
class ExperienceCheck:
    """You qualify when your years fall inside the range the job asks for.

    A job wanting 0-2 years is not a match for someone with 8, so the upper
    bound matters as much as the lower one. An open ended maximum means no
    ceiling.
    """

    years: float
    field: Field = Field.EXPERIENCE

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict:
        low, high = job.experience_min, job.experience_max
        if low is None and high is None:
            return ctx.unverifiable(job, self.field)
        if (low is not None and self.years < low) or (high is not None and self.years > high):
            return Verdict(False, reason=f'experience does not fit {self.years} years')
        return KEEP


@dataclass(frozen=True)
class PostedCheck:
    days: int
    field: Field = Field.POSTED

    def __call__(self, job: Job, ctx: FilterContext) -> Verdict:
        if not job.posted:
            return ctx.unverifiable(job, self.field)
        try:
            posted = datetime.strptime(job.posted[:10], '%Y-%m-%d').date()
        except ValueError:
            # the board published a date and we could not read it, which is a
            # parse failure of ours, not a board that stays silent
            return Verdict(ctx.policy.keep_unknown, flags.DATE_UNKNOWN)
        if (ctx.today - posted).days <= self.days:
            return KEEP
        return Verdict(False, reason='posted date outside allowed window')


# -- the configuration ----------------------------------------------------


def _values(wanted: str | Sequence[str] | None) -> tuple[str, ...]:
    """One filter or several, as a tuple. Empty means "does not constrain"."""
    if wanted is None:
        return ()
    if isinstance(wanted, str):
        wanted = [wanted]
    return tuple(value for value in wanted if value and value.strip())


@dataclass
class JobFilter:
    """Every field is optional; unset fields simply do not constrain anything.

    `title` and `company` take one value or several. Several match when *any* of
    them does, because a shortlist worth having spans more title families than
    one string of words can express - "AI", "ML", "Machine Learning" and "Data
    Scientist" are one search to a person and four to a text match.
    """

    title: str | Sequence[str] | None = None
    company: str | Sequence[str] | None = None
    location: str | None = None
    min_salary: float | None = None  # annual, in `currency`
    currency: str | None = None
    # how to compare pay quoted in another currency:
    #   'ppp'    - purchasing power, the fair cross-country comparison
    #   'market' - today's exchange rate
    #   'strict' - refuse to compare, flag the mismatch
    salary_basis: str = 'ppp'
    # where FX and PPP figures come from. Here in the domain, None means no
    # figures at all: pay in another currency is reported as unconverted.
    # applicant.filters.JobFilter fills it with the shared live cache instead.
    rates: RateTable | None = None
    experience: float | None = None  # years you have
    posted_within_days: int | None = None
    keep_unknown: bool = True  # unverifiable jobs survive, flagged
    # only consulted when keep_unknown is False. A board that never publishes a
    # field is not the same as a posting that declined to state it: set this to
    # drop the second and keep the first, rather than deleting whole boards.
    keep_unpublished: bool = False

    def checks(self) -> list[Check]:
        """The criteria this filter sets, in the order they are applied."""
        found: list[Check] = []
        if title := _values(self.title):
            found.append(TextCheck(Field.TITLE, title))
        if company := _values(self.company):
            found.append(TextCheck(Field.COMPANY, company))
        if self.location:
            found.append(LocationCheck(self.location))
        if self.min_salary is not None:
            found.append(SalaryCheck(self.min_salary, self.currency, self.salary_basis))
        if self.experience is not None:
            found.append(ExperienceCheck(self.experience))
        if self.posted_within_days is not None:
            found.append(PostedCheck(self.posted_within_days))
        return found

    def context(self, today: date | None = None) -> FilterContext:
        return FilterContext(
            today=today or datetime.now(timezone.utc).date(),
            rates=self._rate_table(),
            policy=SilencePolicy(self.keep_unknown, self.keep_unpublished),
            capability_of=self._capability_of(),
        )

    def _rate_table(self) -> RateTable:
        return self.rates if self.rates is not None else EMPTY

    def _capability_of(self) -> CapabilityOf:
        return _unknown_board

    def currencies(self) -> set[str]:
        """The currency every salary here is compared in, if there is one."""
        return {self.currency.upper()} if self.currency and self.min_salary is not None else set()

    def matches(
        self, job: Job, today: date | None = None, skip: Collection[str] = ()
    ) -> tuple[bool, list[str]]:
        """-> (keep, flags). Flags say what could not actually be checked.

        `skip` names checks the board already did itself. Re-checking those
        locally is not merely wasted work, it is wrong: boards answer a country
        search with bare city names ("Bengaluru"), so a second pass over
        `job.location` would throw away every correct result.

        A flag ending `-unknown` means the posting did not say; one ending
        `-unpublished` means its board never says.
        """
        ctx = self.context(today)
        found: list[str] = []
        for check in self.checks():
            if check.field in skip:
                continue
            verdict = check(job, ctx)
            if verdict.flag:
                found.append(verdict.flag)
            if not verdict.keep:
                if verdict.reason:
                    # the first thing to check when a search comes back empty;
                    # debug only - there is one line per job
                    logger.debug(f'{job.source}: dropped {job.id!r}: {verdict.reason}')
                return False, found
        return True, found
