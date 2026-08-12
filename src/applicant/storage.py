"""Where jobs and applications are kept between runs.

Two files, both append-oriented:

* `job_listing.json` - every job ever seen, merged on write, newest wins.
* `applied_jobs.csv` - one row per application, never the same posting twice.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone

from .models import Job
from .salary import parse_salary

APPLIED_COLUMNS = [
    'applied_at',
    'status',
    'source',
    'id',
    'title',
    'company',
    'location',
    'salary',
    'salary_annual_low',
    'salary_annual_high',
    'currency',
    'experience_min',
    'experience_max',
    'posted',
    'url',
    'flags',
    'note',
]

# what `status` may hold in applied_jobs.csv
STATUSES = ('applied', 'needs_manual_apply', 'would_apply', 'failed')


def _key(item: dict) -> tuple:
    """Identity for deduping.

    Boards that hand out an id are keyed on it; aggregators that do not (Google
    Jobs) fall back to the posting itself, otherwise every one of their rows
    would collapse into a single (source, None) entry.
    """
    if item.get('id'):
        return item.get('source'), item['id']
    return item.get('source'), item.get('title'), item.get('company'), item.get('location')


_PUNCTUATION = re.compile(r'[^a-z0-9]+')


def _flatten(value: str | None) -> str:
    return _PUNCTUATION.sub(' ', (value or '').lower()).strip()


def fingerprint(item: dict) -> str | None:
    """Identity *across* boards: one job, however many boards carried it.

    `_key` is per source by design - each board's copy of a posting is worth
    storing, since they carry different fields and only some carry a url. But
    they are still one job, and applying to it three times is three emails to
    the same employer.

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


def load_jobs(filepath: str) -> list[Job]:
    """Read a job listing file. Missing or malformed reads as empty."""
    try:
        with open(filepath, encoding='utf-8') as file:
            stored = json.load(file).get('list', [])
    except (FileNotFoundError, ValueError):
        return []
    return [Job.from_dict(item) for item in stored]


def save_jobs(jobs: Iterable[Job], filepath: str) -> int:
    """Merge into an existing file, newest write winning. Returns the total stored."""
    merged = {}
    try:
        with open(filepath, encoding='utf-8') as file:
            for item in json.load(file).get('list', []):
                merged[_key(item)] = item
    except (FileNotFoundError, ValueError):
        pass

    for job in jobs:
        item = job.to_dict()
        merged[_key(item)] = item

    with open(filepath, 'w', encoding='utf-8') as file:
        json.dump({'list': list(merged.values())}, file, indent=2, ensure_ascii=False)
    return len(merged)


class ApplicationLog:
    """Append only record of what was applied to, as a spreadsheet import.

    Written as CSV so it drops straight into Google Sheets via File > Import;
    the columns are stable so re-imports line up.
    """

    def __init__(self, path: str = 'applied_jobs.csv'):
        self.path = path

    def existing_keys(self) -> set[tuple[str | None, str | None]]:
        return {(row.get('source'), row.get('id')) for row in self.rows()}

    def existing_fingerprints(self) -> set[str]:
        """What has been applied to already, whichever board it came from."""
        found = (fingerprint(row) for row in self.rows())
        return {value for value in found if value}

    def record(self, entries: Iterable[tuple[Job, str, str]]) -> int:
        """entries: iterable of (job, status, note). Returns rows written."""
        rows = self.rows()
        seen = {(row.get('source'), row.get('id')) for row in rows}
        boards_of: dict[str, set[str | None]] = {}
        for row in rows:
            mark = fingerprint(row)
            if mark:
                boards_of.setdefault(mark, set()).add(row.get('source'))
        fresh = []

        for job, status, note in entries:
            if (job.source, job.id) in seen:
                continue
            # The same posting under another board's id is still one job. Two ids
            # on the *same* board are not: there the id is authoritative, and
            # collapsing them would throw away a posting the board thinks is real.
            mark = fingerprint(job.to_dict())
            if mark and boards_of.get(mark, set()) - {job.source}:
                continue
            seen.add((job.source, job.id))
            if mark:
                boards_of.setdefault(mark, set()).add(job.source)
            salary = parse_salary(job.salary)
            fresh.append(
                {
                    'applied_at': datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                    'status': status,
                    'source': job.source,
                    'id': job.id,
                    'title': job.title,
                    'company': job.company,
                    'location': job.location,
                    'salary': job.salary,
                    'salary_annual_low': salary.annual_low if salary else None,
                    'salary_annual_high': salary.annual_high if salary else None,
                    'currency': salary.currency if salary else None,
                    'experience_min': job.experience_min,
                    'experience_max': job.experience_max,
                    'posted': job.posted,
                    'url': job.url,
                    'flags': ' '.join(job.flags),
                    'note': note,
                }
            )

        if not fresh:
            return 0

        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        # utf-8-sig so Sheets and Excel both read the currency symbols correctly
        with open(self.path, 'a', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=APPLIED_COLUMNS)
            if is_new:
                writer.writeheader()
            writer.writerows(fresh)
        return len(fresh)

    def rows(self) -> list[dict[str, str]]:
        """Everything recorded so far, in the order it was written."""
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding='utf-8-sig', newline='') as handle:
            return list(csv.DictReader(handle))

    def counts(self) -> dict[str, int]:
        """status -> how many rows hold it. The at-a-glance view of a run."""
        tally: dict[str, int] = {}
        for row in self.rows():
            status = row.get('status') or 'unknown'
            tally[status] = tally.get(status, 0) + 1
        return tally
