"""What the services report while they work.

A service never prints. It logs its commentary, returns its answer, and - for
anything a caller may want to show as it happens - emits one of these events
through the `emit` callback it was given. The CLI's renderer turns them into
terminal output; a program embedding the library can turn them into a progress
bar, JSON lines, or nothing at all (`ignore`, the default).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Event:
    """Base class, so a renderer can accept every event and pick what it shows."""


@dataclass(frozen=True)
class SourceFailed(Event):
    """One source failed; the others carry on.

    `expected` is False for a failure the source did not anticipate - a bug or
    an environment fault rather than a bot check - whose traceback went to the
    debug log. `subject` is what was being fetched, where there is one.
    """

    source: str
    error: BaseException
    expected: bool = True
    subject: str | None = None


@dataclass(frozen=True)
class BoardSearched(Event):
    """One job board read: how many survived the filter out of how many seen."""

    source: str
    kept: int
    seen: int


@dataclass(frozen=True)
class RatingFetched(Event):
    source: str
    rating: Any  # applicant.reviews.CompanyRating


@dataclass(frozen=True)
class FinancialsTracked(Event):
    """A company's funding, recorded; `changes` is what moved since last time."""

    source: str
    financials: Any  # applicant.financials.CompanyFinancials
    changes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FactorFetched(Event):
    """One PPP factor asked for. `entry` is None when none came back."""

    country: str
    entry: dict | None
    skipped: bool = False


Emit = Callable[[Event], None]


def ignore(event: Event) -> None:
    """The default `emit`: a library call shows nothing unless asked to."""
    del event
