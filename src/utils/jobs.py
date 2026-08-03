"""Shared pieces for the job board scrapers.

Every board module returns the same Job objects, so run.py and any caller can
mix sources without special casing them.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')

BROWSER_ARGS = ['--disable-blink-features=AutomationControlled', '--disable-extensions']


class JobsError(Exception):
    """Base class for every failure raised by the job board clients."""


class BlockedError(JobsError):
    """A bot check, captcha or login wall stopped the scrape."""


@dataclass
class Job:
    source: str
    title: str
    id: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    posted: str | None = None            # ISO date where the board gives us one
    posted_text: str | None = None       # what the board actually said, e.g. '6 days ago'
    employment_type: str | None = None
    salary: str | None = None
    via: str | None = None               # originating board, for aggregators
    remote: bool | None = None
    easy_apply: bool | None = None
    # why a job survived a filter it could not be checked against,
    # e.g. 'salary-unknown' - see utils.jobsearch
    flags: list = field(default_factory=list)

    def to_dict(self):
        return {
            'source': self.source,
            'id': self.id,
            'title': self.title,
            'company': self.company,
            'location': self.location,
            'url': self.url,
            'posted': self.posted,
            'posted_text': self.posted_text,
            'employment_type': self.employment_type,
            'salary': self.salary,
            'via': self.via,
            'remote': self.remote,
            'easy_apply': self.easy_apply,
            'flags': list(self.flags),
        }


def _key(item):
    """Identity for deduping.

    Boards that hand out an id are keyed on it; aggregators that do not (Google
    Jobs) fall back to the posting itself, otherwise every one of their rows
    would collapse into a single (source, None) entry.
    """
    if item.get('id'):
        return item.get('source'), item['id']
    return item.get('source'), item.get('title'), item.get('company'), item.get('location')


def save_jobs(jobs, filepath):
    """Merge into an existing file, newest write winning."""
    merged = {}
    try:
        with open(filepath, 'r', encoding='utf-8') as file:
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


RELATIVE = re.compile(r'(\d+)\+?\s*(minute|hour|day|week|month)s?\s*ago', re.IGNORECASE)
UNITS = {'minute': 1 / 1440.0, 'hour': 1 / 24.0, 'day': 1.0, 'week': 7.0, 'month': 30.0}


def relative_to_iso(text, now=None):
    """'6 days ago' -> '2026-07-28'. Returns None when the text is not relative."""
    if not text:
        return None
    match = RELATIVE.search(text)
    if not match:
        return None
    days = int(match.group(1)) * UNITS[match.group(2).lower()]
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=days)).date().isoformat()


def epoch_to_iso(value):
    """Indeed hands out epoch milliseconds."""
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, timezone.utc).date().isoformat()
    except (ValueError, OSError, OverflowError):
        return None


# Akamai and friends fingerprint Playwright's bundled Chromium and return
# "Access Denied" outright, while an installed Chrome or Edge sails through.
# None means the bundled build, which is the last resort.
CHANNELS = ('chrome', 'msedge', None)


@contextmanager
def browser(profile_dir=None, headless=True, timeout=45000, channels=CHANNELS):
    """A Playwright page, persistent when a profile dir is given.

    A persistent profile is what lets a board remember that a human already
    cleared its bot check.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise JobsError('this source needs playwright: '
                        'uv sync && uv run playwright install chromium')

    options = dict(headless=headless, args=BROWSER_ARGS)
    context_options = dict(user_agent=USER_AGENT, locale='en-US',
                           viewport={'width': 1366, 'height': 900})

    with sync_playwright() as driver:
        closer = context = None
        failures = []

        for channel in channels:
            extra = {'channel': channel} if channel else {}
            try:
                if profile_dir:
                    context = driver.chromium.launch_persistent_context(
                        profile_dir, **dict(options, **context_options, **extra))
                    closer = context
                else:
                    instance = driver.chromium.launch(**dict(options, **extra))
                    context = instance.new_context(**context_options)
                    closer = instance
                break
            except Exception as error:  # channel is simply not installed
                failures.append('{}: {}'.format(channel or 'bundled',
                                                str(error).split('\n')[0][:80]))

        if context is None:
            raise JobsError('could not launch a browser -\n  ' + '\n  '.join(failures))

        page = context.pages[0] if profile_dir and context.pages else context.new_page()
        page.set_default_timeout(timeout)
        try:
            yield page
        finally:
            closer.close()


def looks_blocked(page):
    """Cheap shared check for the interstitials these boards throw."""
    try:
        title = (page.title() or '').lower()
    except Exception:
        return False
    if any(flag in title for flag in ('just a moment', 'attention required',
                                      'unusual traffic', 'access denied')):
        return True
    return page.locator('#challenge-platform, #cf-challenge-running, form#captcha-form').count() > 0
