"""Shared pieces for the job board scrapers.

Every board module returns the same Job objects, so run.py and any caller can
mix sources without special casing them.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36')

BROWSER_ARGS = ['--disable-blink-features=AutomationControlled', '--disable-extensions']


class JobsError(Exception):
    """Base class for every failure raised by the job board clients."""


class BlockedError(JobsError):
    """A bot check, captcha or login wall stopped the scrape."""


EXPERIENCE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)'
    r'|(\d+(?:\.\d+)?)\s*\+\s*(?:yrs?|years?)'
    r'|(?:min(?:imum)?|at least)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)'
    r'|(\d+(?:\.\d+)?)\s*(?:yrs?|years?)',
    re.IGNORECASE)
FRESHER = re.compile(r'\bfresher|\bentry[ -]level|\bno experience\b|\bgraduate trainee\b',
                     re.IGNORECASE)


def parse_experience(text):
    """'0-2 Yrs' -> (0.0, 2.0); '5+ years' -> (5.0, None). (None, None) if absent.

    An open ended maximum is meaningful: '5+ years' must not become '5 to 5'.
    """
    if not text:
        return None, None
    if FRESHER.search(text):
        return 0.0, 0.0

    match = EXPERIENCE.search(text)
    if not match:
        return None, None
    low_high, high, plus, minimum, exact = match.groups()
    if low_high is not None:
        return float(low_high), float(high)
    if plus is not None:
        return float(plus), None
    if minimum is not None:
        return float(minimum), None
    return float(exact), float(exact)


class Job(BaseModel):
    """A posting, normalised across every board."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

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
    experience_text: str | None = None   # what the board said, e.g. '0-2 Yrs'
    experience_min: float | None = Field(default=None, ge=0, le=60)
    experience_max: float | None = Field(default=None, ge=0, le=60)
    via: str | None = None               # originating board, for aggregators
    remote: bool | None = None
    easy_apply: bool | None = None
    # why a job survived a filter it could not be checked against,
    # e.g. 'salary-unknown' - see utils.jobsearch
    flags: list[str] = Field(default_factory=list)

    @field_validator('*', mode='before')
    @classmethod
    def _blank_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode='after')
    def _fill_experience(self):
        """Derive the year range from whatever text the board gave us."""
        if self.experience_min is None and self.experience_max is None:
            low, high = parse_experience(self.experience_text or self.title)
            if low is not None or high is not None:
                # assignment validation is on, so set via __dict__ to avoid recursing
                self.__dict__['experience_min'] = low
                self.__dict__['experience_max'] = high
        if (self.experience_min is not None and self.experience_max is not None
                and self.experience_max < self.experience_min):
            self.__dict__['experience_min'], self.__dict__['experience_max'] = (
                self.experience_max, self.experience_min)
        return self

    def to_dict(self):
        return self.model_dump()


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
