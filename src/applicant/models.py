"""The shared vocabulary of the job boards.

Every board module returns the same `Job` objects and raises the same errors, so
`applicant.search` and any other caller can mix sources without special casing them.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class JobsError(Exception):
    """Base class for every failure raised by the job board clients."""


class BlockedError(JobsError):
    """A bot check, captcha or login wall stopped the scrape."""


EXPERIENCE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:-|to|–)\s*(\d+(?:\.\d+)?)\s*\+?\s*(?:yrs?|years?)'
    r'|(\d+(?:\.\d+)?)\s*\+\s*(?:yrs?|years?)'
    r'|(?:min(?:imum)?|at least)\s*(\d+(?:\.\d+)?)\s*(?:yrs?|years?)'
    r'|(\d+(?:\.\d+)?)\s*(?:yrs?|years?)',
    re.IGNORECASE,
)
FRESHER = re.compile(
    r'\bfresher|\bentry[ -]level|\bno experience\b|\bgraduate trainee\b', re.IGNORECASE
)


# the bounds the model enforces on experience_min / experience_max
YEAR_LIMIT = 60


def _in_range(value: float | None) -> bool:
    return value is None or 0 <= value <= YEAR_LIMIT


def parse_experience(text: str | None) -> tuple[float | None, float | None]:
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


def experience_from(text: str | None) -> tuple[str, float | None, float | None] | None:
    """What a free text description says about required experience.

    -> (the phrase it used, low, high), or None when it says nothing usable.
    The phrase comes back so a posting can record where its numbers came from
    rather than storing a whole job description in `experience_text`.
    """
    if not text:
        return None
    match = FRESHER.search(text) or EXPERIENCE.search(text)
    if match is None:
        return None

    phrase = match.group(0).strip()
    low, high = parse_experience(phrase)
    if low is None and high is None:
        return None
    if not _in_range(low) or not _in_range(high):
        return None
    return phrase, low, high


class Job(BaseModel):
    """A posting, normalised across every board."""

    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    source: str
    title: str
    id: str | None = None
    company: str | None = None
    location: str | None = None
    url: str | None = None
    posted: str | None = None  # ISO date where the board gives us one
    posted_text: str | None = None  # what the board actually said, e.g. '6 days ago'
    employment_type: str | None = None
    salary: str | None = None
    experience_text: str | None = None  # what the board said, e.g. '0-2 Yrs'
    experience_min: float | None = Field(default=None, ge=0, le=YEAR_LIMIT)
    experience_max: float | None = Field(default=None, ge=0, le=YEAR_LIMIT)
    via: str | None = None  # originating board, for aggregators
    remote: bool | None = None
    easy_apply: bool | None = None
    # why a job survived a filter it could not be checked against,
    # e.g. 'salary-unknown' - see applicant.filters
    flags: list[str] = Field(default_factory=list)

    @field_validator('*', mode='before')
    @classmethod
    def _blank_to_none(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode='before')
    @classmethod
    def _fill_experience(cls, data):
        """Derive the year range from whatever text the board gave us.

        Done before validation rather than after, so a derived range is held to
        the same bounds as one passed in explicitly, and so nothing has to be
        written past `validate_assignment`.
        """
        if not isinstance(data, dict):
            return data

        low, high = data.get('experience_min'), data.get('experience_max')

        if low is None and high is None:
            low, high = parse_experience(data.get('experience_text') or data.get('title'))
            # a posting saying "100 years" is a typo, not a requirement: read it
            # as unknown rather than failing the whole scrape on it
            if not _in_range(low) or not _in_range(high):
                low = high = None

        if low is not None and high is not None and high < low:
            low, high = high, low

        return {**data, 'experience_min': low, 'experience_max': high}

    def to_dict(self) -> dict:
        return self.model_dump()

    @classmethod
    def from_dict(cls, item: dict) -> Job:
        """Build a Job from a stored record, ignoring fields we no longer know."""
        known = set(cls.model_fields)
        return cls(**{key: value for key, value in item.items() if key in known})
