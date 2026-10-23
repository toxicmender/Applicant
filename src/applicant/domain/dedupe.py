"""When two postings are one job, and which copy of it to keep.

Two notions of identity, deliberately different:

* `key` is identity *within* a board: its own id where it hands one out. Each
  board's copy of a posting is worth storing - they carry different fields,
  and only some carry a url.
* `fingerprint` is identity *across* boards: one job, however many boards
  carried it. Applying to it three times is three emails to one employer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from . import flags
from .job import Job

_PUNCTUATION = re.compile(r'[^a-z0-9]+')


def _flatten(value: str | None) -> str:
    return _PUNCTUATION.sub(' ', (value or '').lower()).strip()


def key(item: Mapping[str, Any]) -> tuple:
    """Identity for deduping within a board.

    Boards that hand out an id are keyed on it; aggregators that do not (Google
    Jobs) fall back to the posting itself, otherwise every one of their rows
    would collapse into a single (source, None) entry.
    """
    if item.get('id'):
        return item.get('source'), item['id']
    return item.get('source'), item.get('title'), item.get('company'), item.get('location')


def fingerprint(item: Mapping[str, Any]) -> str | None:
    """Identity *across* boards: one job, however many boards carried it.

    The city alone, not the whole location string: boards write "Bengaluru",
    "Bengaluru, Karnataka" and "Bengaluru, India" for the same office. Returns
    None when company or title is missing, because a fingerprint that is not
    sure is worse than none.
    """
    company, title = _flatten(item.get('company')), _flatten(item.get('title'))
    if not company or not title:
        return None
    city = _flatten((item.get('location') or '').split(',')[0])
    return '|'.join((company, title, city))


def reach(job: Job) -> int:
    """How far this copy of a posting gets you, highest first.

    Easy Apply can be automated; a url can at least be opened; a Google Jobs
    row has neither and leaves you searching for it again by hand.
    """
    if job.source == 'linkedin' and job.url and job.easy_apply is not False:
        return 2
    return 1 if job.url else 0


def merge_flags(winner: Job, loser: Job) -> list[str]:
    """The winner's own flags, then every board the job was also on."""
    elsewhere = dict.fromkeys(
        flag for flag in (*winner.flags, *loser.flags) if flags.is_also_on(flag)
    )
    elsewhere[flags.also_on(loser.source)] = None
    own = [flag for flag in winner.flags if not flags.is_also_on(flag)]
    return [*own, *elsewhere]


def one_per_job(jobs: Iterable[Job]) -> list[Job]:
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

        winner, loser = (job, rival) if reach(job) > reach(rival) else (rival, job)
        if winner is not rival:
            ordered[ordered.index(rival)] = winner
            best[mark] = winner
        # the loser may itself have outlived an earlier copy, so carry its
        # record of them across rather than losing it with the object
        winner.flags = merge_flags(winner, loser)

    return ordered
