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
from typing import TYPE_CHECKING, Protocol

from .boards import Capability
from .filters import JobFilter
from .models import Job, JobsError
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
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> list[Job]:
        """Search every configured board and return the filtered, merged results.

        `limit` is per board *before* filtering, so a strict filter returns fewer.
        `want` asks for a number of *survivors* instead: each board is re-read
        with a larger limit until that many get through, the board runs out, or
        `max_rounds` is reached. Left as None nothing changes and each board is
        read exactly once.
        """
        filters = filters or JobFilter()
        collected: list[Job] = []

        for name in self.sources:
            client = self._client(name)
            try:
                kept, seen = self._from_board(
                    client, name, keywords, filters, limit, want, max_rounds
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

            if want is None or len(kept) >= want:
                break
            if seen < pull:
                # the board gave us everything it had; asking again is pointless
                break
            if round_number + 1 < max_rounds:
                pull *= 2
                print('{}: {} of {} match, reading {}'.format(name, len(kept), seen, pull))

        return (kept[:want] if want else kept), seen

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
