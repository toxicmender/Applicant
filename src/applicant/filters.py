"""Deciding which postings survive, and saying what could not be checked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from .boards import capability
from .models import Job
from .money import Rates, convert
from .places import within
from .salary import parse_salary


@dataclass
class JobFilter:
    """Every field is optional; unset fields simply do not constrain anything."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    min_salary: float | None = None  # annual, in `currency`
    currency: str | None = None
    # how to compare pay quoted in another currency:
    #   'ppp'    - purchasing power, the fair cross-country comparison
    #   'market' - today's exchange rate
    #   'strict' - refuse to compare, flag the mismatch
    salary_basis: str = 'ppp'
    # where FX and PPP figures come from. None uses the shared cache, which
    # fetches on a miss; pass Rates(offline=True) to keep a run off the network.
    rates: Rates | None = None
    experience: float | None = None  # years you have
    posted_within_days: int | None = None
    keep_unknown: bool = True  # unverifiable jobs survive, flagged
    # only consulted when keep_unknown is False. A board that never publishes a
    # field is not the same as a posting that declined to state it: set this to
    # drop the second and keep the first, rather than deleting whole boards.
    keep_unpublished: bool = False

    def matches(
        self, job: Job, today: date | None = None, skip: tuple[str, ...] | list[str] = ()
    ) -> tuple[bool, list[str]]:
        """-> (keep, flags). Flags say what could not actually be checked.

        `skip` names checks the board already did itself. Re-checking those
        locally is not merely wasted work, it is wrong: boards answer a country
        search with bare city names ("Bengaluru"), so a second pass over
        `job.location` would throw away every correct result.

        A flag ending `-unknown` means the posting did not say; one ending
        `-unpublished` means its board never says, which `job.source` decides
        through `applicant.boards.capability`.
        """
        flags: list[str] = []

        for value, field_name in ((self.title, 'title'), (self.company, 'company')):
            if not value or field_name in skip:
                continue
            actual = getattr(job, field_name) or ''
            if not self._text_matches(value, actual):
                return False, flags

        if self.location and 'location' not in skip:
            keep, flag = self._location_ok(job, self.location)
            if flag:
                flags.append(flag)
            if not keep:
                return False, flags

        if self.min_salary is not None and 'salary' not in skip:
            keep, flag = self._salary_ok(job, self.min_salary)
            if flag:
                flags.append(flag)
            if not keep:
                return False, flags

        if self.experience is not None and 'experience' not in skip:
            keep, flag = self._experience_ok(job, self.experience)
            if flag:
                flags.append(flag)
            if not keep:
                return False, flags

        if self.posted_within_days is not None and 'posted' not in skip:
            keep, flag = self._date_ok(job, self.posted_within_days, today)
            if flag:
                flags.append(flag)
            if not keep:
                return False, flags

        return True, flags

    def _text_matches(self, wanted: str, actual: str) -> bool:
        """Every word of the filter must appear, in any order."""
        actual = actual.lower()
        return all(word in actual for word in wanted.lower().split())

    def _location_ok(self, job: Job, wanted: str) -> tuple[bool, str | None]:
        """Text first, then containment: 'India' has to accept 'Bengaluru'.

        Only the boards spell a country out; `apply` reads a stored file where
        the location is whatever the board said, so without this a country
        filter empties the worklist instead of narrowing it.
        """
        verdict = within(wanted, job.location)
        if verdict is None:
            return self.keep_unknown, 'location-unverified'
        return verdict, None

    def _unverifiable(self, job: Job, field: str, stem: str) -> tuple[bool, str]:
        """What becomes of a posting that could not be checked, and what to call it.

        Silence has two very different sources. A posting on a board that
        publishes experience and states none is being evasive; a posting on a
        board that never publishes it at all has said nothing wrong. Only the
        first is a reason to drop anything, which is why `--strict` alone -
        drop everything unverifiable - deletes three of the four boards the
        moment an experience filter is set.
        """
        if field not in capability(job.source).publishes:
            return self.keep_unknown or self.keep_unpublished, '{}-unpublished'.format(stem)
        return self.keep_unknown, '{}-unknown'.format(stem)

    def _experience_ok(self, job: Job, years: float) -> tuple[bool, str | None]:
        """You qualify when your years fall inside the range the job asks for.

        A job wanting 0-2 years is not a match for someone with 8, so the upper
        bound matters as much as the lower one. An open ended maximum means no
        ceiling.
        """
        low, high = job.experience_min, job.experience_max
        if low is None and high is None:
            return self._unverifiable(job, 'experience', 'experience')
        if low is not None and years < low:
            return False, None
        if high is not None and years > high:
            return False, None
        return True, None

    def _salary_ok(self, job: Job, minimum: float) -> tuple[bool, str | None]:
        salary = parse_salary(job.salary)
        if salary is None:
            return self._unverifiable(job, 'salary', 'salary')

        top = salary.annual_high
        if top is None:
            return self._unverifiable(job, 'salary', 'salary')

        flag = None
        if self.currency and salary.currency and salary.currency != self.currency:
            if self.salary_basis == 'strict':
                return self.keep_unknown, 'salary-currency-mismatch'
            top, note = convert(
                top, salary.currency, self.currency, basis=self.salary_basis, table=self.rates
            )
            if top is None:
                return self.keep_unknown, note or 'salary-currency-mismatch'
            flag = note  # e.g. fell back off ppp to market
        elif self.currency and not salary.currency:
            # a bare number with no symbol - assume it is already in `currency`
            flag = 'salary-currency-assumed'

        return top >= minimum, flag

    def _date_ok(
        self, job: Job, within_days: int, today: date | None = None
    ) -> tuple[bool, str | None]:
        if not job.posted:
            return self._unverifiable(job, 'posted', 'date')
        try:
            posted = datetime.strptime(job.posted[:10], '%Y-%m-%d').date()
        except ValueError:
            # the board published a date and we could not read it, which is a
            # parse failure of ours, not a board that stays silent
            return self.keep_unknown, 'date-unknown'
        today = today or datetime.now(timezone.utc).date()
        return (today - posted).days <= within_days, None
