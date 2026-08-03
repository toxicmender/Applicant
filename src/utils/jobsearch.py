"""One interface over the job boards: search, filter, apply, record.

    from utils.jobsearch import Jobs, JobFilter

    board = Jobs()
    hits = board.search('python developer', JobFilter(location='India', posted_within_days=7))
    board.apply(hits, log='applied_jobs.csv')

Filters are pushed down to each board where it supports them natively (location,
keywords, date posted) and applied locally for the rest, so results are consistent
no matter which board they came from.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from .jobs import Job, JobsError, save_jobs

SOURCES = ('linkedin', 'indeed', 'naukri', 'googlejobs')

# boards that can filter by age server side; the rest are filtered locally
NATIVE_DATE = ('linkedin', 'indeed')

CURRENCIES = [
    (re.compile(r'₹|\bINR\b|\bRs\.?\b', re.IGNORECASE), 'INR'),
    (re.compile(r'\$|\bUSD\b', re.IGNORECASE), 'USD'),
    (re.compile(r'€|\bEUR\b', re.IGNORECASE), 'EUR'),
    (re.compile(r'£|\bGBP\b', re.IGNORECASE), 'GBP'),
]

# how many of each pay period make a year
PERIODS = [
    (re.compile(r'\b(an?\s+)?hour|\bhourly\b|\b/\s*hr\b|\bper hour\b', re.IGNORECASE), 2080),
    (re.compile(r'\b(a\s+)?day\b|\bdaily\b|\bper day\b', re.IGNORECASE), 260),
    (re.compile(r'\b(a\s+)?week\b|\bweekly\b|\bper week\b', re.IGNORECASE), 52),
    (re.compile(r'\b(a\s+)?month\b|\bmonthly\b|\bPM\b|\bp\.?m\.?\b', re.IGNORECASE), 12),
    (re.compile(r'\b(a\s+)?year\b|\byearly\b|\bannum\b|\bannual\b|\bPA\b|\bp\.?a\.?\b', re.IGNORECASE), 1),
]

MULTIPLIERS = [
    (re.compile(r'^(cr|crore)', re.IGNORECASE), 10_000_000),
    (re.compile(r'^(l|lac|lacs|lakh|lakhs)', re.IGNORECASE), 100_000),
    (re.compile(r'^k\b', re.IGNORECASE), 1_000),
    (re.compile(r'^m\b', re.IGNORECASE), 1_000_000),
]

AMOUNT = re.compile(r'(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z]*)')


@dataclass
class Salary:
    low: float | None = None
    high: float | None = None
    currency: str | None = None
    period_per_year: int = 1

    @property
    def annual_high(self):
        return self.high * self.period_per_year if self.high is not None else None

    @property
    def annual_low(self):
        return self.low * self.period_per_year if self.low is not None else None


def parse_salary(text):
    """'2-2.5 Lacs PA' -> Salary(200000, 250000, 'INR', 1). None when unparseable.

    Boards write pay a dozen different ways and most postings state none at all,
    so callers must treat None as 'unknown', never as 'zero'.
    """
    if not text:
        return None
    cleaned = text.replace('–', '-').replace('—', '-').replace('−', '-')
    if re.search(r'not disclosed|unpaid|negotiable', cleaned, re.IGNORECASE):
        return None

    currency = None
    for pattern, code in CURRENCIES:
        if pattern.search(cleaned):
            currency = code
            break

    period = 1
    for pattern, factor in PERIODS:
        if pattern.search(cleaned):
            period = factor
            break

    amounts = []
    scale = 1
    for raw, suffix in AMOUNT.findall(cleaned):
        try:
            value = float(raw.replace(',', ''))
        except ValueError:
            continue
        multiplier = 1
        for pattern, factor in MULTIPLIERS:
            if suffix and pattern.match(suffix):
                multiplier = factor
                break
        scale = max(scale, multiplier)
        amounts.append((value, multiplier))

    if not amounts:
        return None

    # ranges carry the unit only on the last number ("2-2.5 Lacs"), so a bare
    # value in the same string takes the unit that was stated
    values = [value * (multiplier if multiplier > 1 else scale)
              for value, multiplier in amounts]

    # 'Lacs' and 'Cr' are themselves rupee units
    if currency is None and scale in (100_000, 10_000_000):
        currency = 'INR'

    # a lone bare number that looks like a year is not pay
    if (len(values) == 1 and currency is None and scale == 1
            and float(values[0]).is_integer() and 1900 <= values[0] <= 2100):
        return None

    return Salary(low=min(values), high=max(values), currency=currency,
                  period_per_year=period)


@dataclass
class JobFilter:
    """Every field is optional; unset fields simply do not constrain anything."""

    title: str | None = None
    company: str | None = None
    location: str | None = None
    min_salary: float | None = None      # annual, in `currency`
    currency: str | None = None
    posted_within_days: int | None = None
    keep_unknown: bool = True            # unverifiable jobs survive, flagged

    def matches(self, job, today=None, skip=()):
        """-> (keep, flags). Flags say what could not actually be checked.

        `skip` names checks the board already did itself. Re-checking those
        locally is not merely wasted work, it is wrong: boards answer a country
        search with bare city names ("Bengaluru"), so a second pass over
        `job.location` would throw away every correct result.
        """
        flags = []

        for value, field_name in ((self.title, 'title'),
                                  (self.company, 'company'),
                                  (self.location, 'location')):
            if not value or field_name in skip:
                continue
            actual = getattr(job, field_name) or ''
            if not self._text_matches(value, actual):
                return False, flags

        if self.min_salary is not None and 'salary' not in skip:
            keep, flag = self._salary_ok(job)
            if flag:
                flags.append(flag)
            if not keep:
                return False, flags

        if self.posted_within_days is not None and 'posted' not in skip:
            keep, flag = self._date_ok(job, today)
            if flag:
                flags.append(flag)
            if not keep:
                return False, flags

        return True, flags

    def _text_matches(self, wanted, actual):
        """Every word of the filter must appear, in any order."""
        actual = actual.lower()
        return all(word in actual for word in wanted.lower().split())

    def _salary_ok(self, job):
        salary = parse_salary(job.salary)
        if salary is None:
            return self.keep_unknown, 'salary-unknown'
        if self.currency and salary.currency and salary.currency != self.currency:
            # no exchange rates are invented here - it is simply not comparable
            return self.keep_unknown, 'salary-currency-mismatch'
        top = salary.annual_high
        if top is None:
            return self.keep_unknown, 'salary-unknown'
        return top >= self.min_salary, None

    def _date_ok(self, job, today=None):
        if not job.posted:
            return self.keep_unknown, 'date-unknown'
        try:
            posted = datetime.strptime(job.posted[:10], '%Y-%m-%d').date()
        except ValueError:
            return self.keep_unknown, 'date-unknown'
        today = today or datetime.now(timezone.utc).date()
        return (today - posted).days <= self.posted_within_days, None


APPLIED_COLUMNS = ['applied_at', 'status', 'source', 'id', 'title', 'company', 'location',
                   'salary', 'salary_annual_low', 'salary_annual_high', 'currency',
                   'posted', 'url', 'flags', 'note']


class ApplicationLog:
    """Append only record of what was applied to, as a spreadsheet import.

    Written as CSV so it drops straight into Google Sheets via File > Import;
    the columns are stable so re-imports line up.
    """

    def __init__(self, path='applied_jobs.csv'):
        self.path = path

    def existing_keys(self):
        keys = set()
        if not os.path.exists(self.path):
            return keys
        with open(self.path, 'r', encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                keys.add((row.get('source'), row.get('id')))
        return keys

    def record(self, entries):
        """entries: iterable of (job, status, note). Returns rows written."""
        seen = self.existing_keys()
        fresh = []

        for job, status, note in entries:
            if (job.source, job.id) in seen:
                continue
            seen.add((job.source, job.id))
            salary = parse_salary(job.salary)
            fresh.append({
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
                'posted': job.posted,
                'url': job.url,
                'flags': ' '.join(job.flags),
                'note': note,
            })

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


class Jobs:
    """The facade: one search across boards, one filter, one apply, one log."""

    def __init__(self, sources=SOURCES, headless=True, linkedin=None):
        self.sources = tuple(sources)
        self.headless = headless
        self._linkedin = linkedin

    def _client(self, name):
        if name == 'linkedin':
            if self._linkedin is None:
                from .linkedin import LinkedIn
                self._linkedin = LinkedIn(headless=self.headless)
            return self._linkedin
        if name == 'indeed':
            from .indeed import Indeed
            return Indeed(headless=self.headless)
        if name == 'naukri':
            from .naukri import Naukri
            return Naukri(headless=self.headless)
        if name == 'googlejobs':
            from .googlejobs import GoogleJobs
            return GoogleJobs(headless=self.headless)
        raise JobsError('unknown source {!r}'.format(name))

    def search(self, keywords, filters=None, limit=25, on_error=None):
        """Search every configured board and return the filtered, merged results.

        `limit` is per board *before* filtering, so a strict filter returns fewer.
        """
        filters = filters or JobFilter()
        collected = []

        for name in self.sources:
            client = self._client(name)
            try:
                jobs = client.search(
                    keywords,
                    filters.location or '',
                    limit=limit,
                    posted_within_days=(filters.posted_within_days
                                        if name in NATIVE_DATE else None),
                )
            except JobsError as error:
                (on_error or self._report)(name, error)
                continue
            finally:
                if name != 'linkedin' and hasattr(client, 'close'):
                    client.close()

            # location always went to the board itself; the date did too on some
            skip = ['location'] + (['posted'] if name in NATIVE_DATE else [])

            kept = []
            for job in jobs:
                keep, flags = filters.matches(job, skip=skip)
                if not keep:
                    continue
                job.flags = flags
                kept.append(job)

            print('{}: {} of {} jobs match'.format(name, len(kept), len(jobs)))
            collected.extend(kept)

        return collected

    def _report(self, name, error):
        print('{}: {}'.format(name, error))

    def apply(self, jobs, log='applied_jobs.csv', filters=None, dry_run=False):
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

        entries = []
        linkedin_targets = []

        for job in jobs:
            if job.source == 'linkedin' and job.url:
                linkedin_targets.append(job)
            else:
                entries.append((job, 'needs_manual_apply',
                                'apply on {} directly'.format(job.via or job.source)))

        if linkedin_targets and not dry_run:
            entries.extend(self._easy_apply(linkedin_targets))
        elif linkedin_targets:
            entries.extend((job, 'would_apply', 'dry run') for job in linkedin_targets)

        written = ApplicationLog(log).record(entries)
        print('{} new rows in {} ({} already recorded)'.format(
            written, log, len(entries) - written))
        return entries

    def _easy_apply(self, jobs):
        from .jobs import save_jobs as _save  # noqa: F401  (kept for symmetry)

        client = self._client('linkedin')
        results = []
        listing = 'applied_via_jobs_interface.json'
        save_jobs(jobs, listing)

        try:
            applied = set(client.easy_apply(listing))
        except JobsError as error:
            print('linkedin: {}'.format(error))
            return [(job, 'failed', str(error)) for job in jobs]

        for job in jobs:
            if job.url in applied:
                results.append((job, 'applied', 'linkedin easy apply'))
            else:
                results.append((job, 'needs_manual_apply',
                                'not easy apply, or a multi step form'))
        return results
