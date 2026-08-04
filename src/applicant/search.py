"""One interface over the job boards: search, filter, apply, record.

    from applicant.search import Jobs
    from applicant.filters import JobFilter

    board = Jobs()
    hits = board.search('python developer', JobFilter(location='India', posted_within_days=7))
    board.apply(hits, log='applied_jobs.csv')

Filters are pushed down to each board where it supports them natively (location,
keywords, date posted) and applied locally for the rest, so results are consistent
no matter which board they came from.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from .boards import Capability
from .filters import JobFilter
from .models import Job, JobsError, experience_from
from .storage import ApplicationLog, fingerprint, save_jobs

if TYPE_CHECKING:
    from .boards.linkedin import LinkedIn


class Board(Protocol):
    """What every board module offers, and all this facade needs of one."""

    capability: Capability

    def search(
        self,
        keywords: str,
        location: str = '',
        limit: int = 25,
        posted_within_days: int | None = None,
    ) -> list[Job]: ...

    def close(self) -> None: ...


SOURCES = ('linkedin', 'indeed', 'naukri', 'googlejobs')


@dataclass
class _Enrichment:
    """Reading postings themselves, and the requests that costs.

    `described` is keyed by posting so a board re-read under `want` does not pay
    for the same page twice; `budget` counts only pages actually fetched.
    """

    budget: int
    described: dict[tuple[str | None, str | None], str | None] = field(default_factory=dict)
    stopped: bool = False


# where jobs handed to _easy_apply are staged for the LinkedIn client to read back
EASY_APPLY_LISTING = 'applied_via_jobs_interface.json'


class Jobs:
    """The facade: one search across boards, one filter, one apply, one log."""

    def __init__(
        self,
        sources: Sequence[str] = SOURCES,
        headless: bool = True,
        linkedin: LinkedIn | None = None,
    ):
        self.sources = tuple(sources)
        self.headless = headless
        self._linkedin = linkedin

    def linkedin(self) -> LinkedIn:
        """The LinkedIn client, which outlives a single search because its
        browser session is what `easy_apply` needs."""
        if self._linkedin is None:
            from .boards.linkedin import LinkedIn

            self._linkedin = LinkedIn(headless=self.headless)
        return self._linkedin

    def _client(self, name: str) -> Board:
        if name == 'linkedin':
            return self.linkedin()
        if name == 'indeed':
            from .boards.indeed import Indeed

            return Indeed(headless=self.headless)
        if name == 'naukri':
            from .boards.naukri import Naukri

            return Naukri(headless=self.headless)
        if name == 'googlejobs':
            from .boards.googlejobs import GoogleJobs

            return GoogleJobs(headless=self.headless)
        raise JobsError('unknown source {!r}'.format(name))

    def search(
        self,
        keywords: str,
        filters: JobFilter | None = None,
        limit: int = 25,
        want: int | None = None,
        max_rounds: int = 4,
        enrich: bool = False,
        enrich_limit: int = 25,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> list[Job]:
        """Search every configured board and return the filtered, merged results.

        `limit` is per board *before* filtering, so a strict filter returns fewer.
        `want` asks for a number of *survivors* instead: each board is re-read
        with a larger limit until that many get through, the board runs out, or
        `max_rounds` is reached. Left as None nothing changes and each board is
        read exactly once.

        `enrich` reads the postings themselves for an experience filter the
        search cards could not answer, at most `enrich_limit` of them.
        """
        filters = filters or JobFilter()
        collected: list[Job] = []
        plan = _Enrichment(budget=enrich_limit) if enrich else None

        for name in self.sources:
            client = self._client(name)
            try:
                kept, seen = self._from_board(
                    client, name, keywords, filters, limit, want, max_rounds, plan
                )
            except JobsError as error:
                (on_error or self._report)(name, error)
                continue
            finally:
                # LinkedIn keeps its session open: apply() reuses it
                if name != 'linkedin':
                    client.close()

            print('{}: {} of {} jobs match'.format(name, len(kept), seen))
            collected.extend(kept)

        return collected

    def _from_board(
        self,
        client: Board,
        name: str,
        keywords: str,
        filters: JobFilter,
        limit: int,
        want: int | None,
        max_rounds: int,
        plan: _Enrichment | None = None,
    ) -> tuple[list[Job], int]:
        """One board's surviving jobs, and how many were read to get them.

        Reading more means asking for a bigger page and re-reading what we
        already saw: the boards page from the top, and Google Jobs only exists
        as a scrolling list, so there is no offset to resume from. That is why
        rounds are capped and why one round remains the default - each extra one
        is a fresh set of requests against a site that is watching for exactly
        that.
        """
        native = client.capability.filters
        # whatever the board filtered for us, we must not filter again -
        # see Capability.filters for why a second pass would be wrong
        skip = sorted(native)
        posted = filters.posted_within_days if 'posted' in native else None

        # when the postings themselves can answer the experience filter, hold it
        # back until they have been read - a card's silence is not an answer
        deferred = (
            plan is not None
            and filters.experience is not None
            and callable(getattr(client, 'describe', None))
        )
        if deferred:
            skip = [*skip, 'experience']

        pull = limit
        kept: list[Job] = []
        seen = 0

        for round_number in range(max(1, max_rounds) if want else 1):
            jobs = client.search(
                keywords, filters.location or '', limit=pull, posted_within_days=posted
            )
            seen = len(jobs)

            kept = []
            for job in jobs:
                keep, flags = filters.matches(job, skip=skip)
                if not keep:
                    continue
                job.flags = flags
                kept.append(job)

            if deferred and plan is not None:
                self._enrich(client, kept, plan)
                kept = self._recheck_experience(kept, filters)

            if want is None or len(kept) >= want:
                break
            if seen < pull:
                # the board gave us everything it had; asking again is pointless
                break
            if round_number + 1 < max_rounds:
                pull *= 2
                print('{}: {} of {} match, reading {}'.format(name, len(kept), seen, pull))

        return (kept[:want] if want else kept), seen

    def _enrich(self, client: Board, jobs: list[Job], plan: _Enrichment) -> None:
        """Read the posting itself where its card never stated the experience.

        Only what a filter actually needs, only for jobs that survived every
        other check, and only up to a budget: this is the slowest path in the
        project and the one most likely to be noticed by a site that would
        rather we were not here. Experience only - a salary read out of free
        prose is as likely to be a relocation allowance as a wage, and nothing
        in this project is ever guessed.
        """
        describe = getattr(client, 'describe')  # noqa: B009 - presence is the check

        for job in jobs:
            if job.experience_min is not None or job.experience_max is not None:
                continue

            key = (job.source, job.id)
            if key not in plan.described:
                if plan.stopped or plan.budget <= 0:
                    continue
                try:
                    plan.described[key] = describe(job)
                except JobsError as error:
                    # one refusal means the next request is worse than useless
                    print('{}: {} (enrichment stopped)'.format(job.source, error))
                    plan.stopped = True
                    continue
                plan.budget -= 1

            found = experience_from(plan.described.get(key))
            if found is None:
                continue

            phrase, low, high = found
            job.experience_text = phrase
            job.experience_min, job.experience_max = low, high
            job.flags = [*job.flags, 'experience-enriched']

    def _recheck_experience(self, jobs: list[Job], filters: JobFilter) -> list[Job]:
        """The check held back for enrichment, now that the postings have been read."""
        check = JobFilter(
            experience=filters.experience,
            keep_unknown=filters.keep_unknown,
            keep_unpublished=filters.keep_unpublished,
        )
        kept = []
        for job in jobs:
            keep, flags = check.matches(job)
            if keep:
                job.flags = [*job.flags, *flags]
                kept.append(job)
        return kept

    def _report(self, name: str, error: Exception) -> None:
        print('{}: {}'.format(name, error))

    def apply(
        self,
        jobs: Iterable[Job],
        log: str = 'applied_jobs.csv',
        filters: JobFilter | None = None,
        dry_run: bool = False,
    ) -> list[tuple[Job, str, str]]:
        """Apply where it is actually possible, and record everything.

        Only LinkedIn Easy Apply can be automated. Postings on the other boards
        hand off to each employer's own form, so they are recorded as
        needs-manual-apply with their url rather than guessed at.
        """
        if filters is not None:
            kept = []
            for job in jobs:
                keep, flags = filters.matches(job)
                if keep:
                    # record why this one could not be fully checked, so the log
                    # says so rather than carrying stale flags from search time
                    job.flags = flags
                    kept.append(job)
            jobs = kept

        jobs = self._one_per_job(jobs)
        entries: list[tuple[Job, str, str]] = []
        linkedin_targets = []

        for job in jobs:
            if job.source == 'linkedin' and job.url:
                linkedin_targets.append(job)
            else:
                entries.append(
                    (
                        job,
                        'needs_manual_apply',
                        'apply on {} directly'.format(job.via or job.source),
                    )
                )

        if linkedin_targets and not dry_run:
            entries.extend(self._easy_apply(linkedin_targets))
        elif linkedin_targets:
            entries.extend((job, 'would_apply', 'dry run') for job in linkedin_targets)

        written = ApplicationLog(log).record(entries)
        print(
            '{} new rows in {} ({} already recorded)'.format(written, log, len(entries) - written)
        )
        return entries

    def _reach(self, job: Job) -> int:
        """How far this copy of a posting gets you, highest first.

        Easy Apply can be automated; a url can at least be opened; a Google Jobs
        row has neither and leaves you searching for it again by hand.
        """
        if job.source == 'linkedin' and job.url and job.easy_apply is not False:
            return 2
        return 1 if job.url else 0

    def _one_per_job(self, jobs: Iterable[Job]) -> list[Job]:
        """Collapse the same posting from several boards into the usable copy.

        Boards are searched independently, so a job advertised on three of them
        arrives three times - and applying to each is three approaches to one
        employer. The copies that lose are recorded on the survivor as
        `also-on-<board>` flags, so nothing disappears silently.

        Only across boards. Two postings from one board are two postings, however
        alike they look: there the board's own id is the authority, and second
        guessing it would throw away a job somebody really did advertise twice.
        """
        best: dict[str, Job] = {}
        ordered: list[Job] = []

        for job in jobs:
            mark = fingerprint(job.to_dict())
            rival = best.get(mark or '')
            if mark is None or rival is None or rival.source == job.source:
                # nothing to collapse against: not enough to be sure it is the
                # same job, the first copy of it, or the same board again
                if mark is not None and rival is None:
                    best[mark] = job
                ordered.append(job)
                continue

            winner, loser = (job, rival) if self._reach(job) > self._reach(rival) else (rival, job)
            if winner is not rival:
                ordered[ordered.index(rival)] = winner
                best[mark] = winner
            # the loser may itself have outlived an earlier copy, so carry its
            # record of them across rather than losing it with the object
            winner.flags = self._merge_flags(winner, loser)

        return ordered

    def _merge_flags(self, winner: Job, loser: Job) -> list[str]:
        elsewhere = dict.fromkeys(
            flag for flag in (*winner.flags, *loser.flags) if flag.startswith('also-on-')
        )
        elsewhere['also-on-{}'.format(loser.source)] = None
        own = [flag for flag in winner.flags if not flag.startswith('also-on-')]
        return [*own, *elsewhere]

    def _easy_apply(self, jobs: list[Job]) -> list[tuple[Job, str, str]]:
        client = self.linkedin()
        results = []
        save_jobs(jobs, EASY_APPLY_LISTING)

        try:
            applied = set(client.easy_apply(EASY_APPLY_LISTING))
        except JobsError as error:
            print('linkedin: {}'.format(error))
            return [(job, 'failed', str(error)) for job in jobs]

        for job in jobs:
            if job.url in applied:
                results.append((job, 'applied', 'linkedin easy apply'))
            else:
                results.append((job, 'needs_manual_apply', 'not easy apply, or a multi step form'))
        return results
