"""What a source does for us, and what it tells us.

Plain data, so the filter can reason about a board's silence without importing
the board: a job read back from a stored file still knows its `source`, and
that is enough to look its capability up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Field(str, Enum):
    """A posting field a board may filter on, or publish.

    A `str` enum, so `Field.SALARY == 'salary'` and `'salary' in capability.filters`
    both hold: code that spelled these as strings keeps working, while a typo
    in new code is an AttributeError rather than a filter that never applies.
    """

    TITLE = 'title'
    COMPANY = 'company'
    LOCATION = 'location'
    SALARY = 'salary'
    EXPERIENCE = 'experience'
    POSTED = 'posted'
    URL = 'url'
    ID = 'id'
    EMPLOYMENT_TYPE = 'employment_type'
    REMOTE = 'remote'
    VIA = 'via'

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Capability:
    """What a board does for us, and what it tells us.

    `filters` names the checks the board applies server side. Re-checking those
    locally is not merely wasted work, it is wrong: a country search answers with
    bare city names, so a second pass over `job.location` throws away every
    correct result.

    `publishes` names the fields its cards actually carry. A board that never
    publishes experience is not the same as a posting that declined to state it,
    and only the second is a reason to drop anything.
    """

    filters: frozenset[Field] = field(default_factory=frozenset)
    publishes: frozenset[Field] = field(default_factory=frozenset)


# A source we do not know is assumed to publish everything, so a missing field
# reads as the posting's silence rather than the board's - which is how every
# filter behaved before capabilities existed.
UNKNOWN = Capability(publishes=frozenset({Field.EXPERIENCE, Field.SALARY, Field.POSTED}))
