"""One module per job board, each returning `applicant.models.Job`.

Clients are imported lazily by `applicant.search.Jobs` so that a board needing a
browser costs nothing until it is actually used. What each board can *do*, on the
other hand, is needed before one is chosen - by the facade, to know which filters
it may skip, and by `applicant.filters`, to know whose silence a missing field is.
So capabilities live here as plain data, importable without pulling in httpx or
Playwright.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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

    filters: frozenset[str] = field(default_factory=frozenset)
    publishes: frozenset[str] = field(default_factory=frozenset)


# Read off the board modules, not off the README:
#
# - LinkedIn's guest card carries a title, company, location and date, and
#   nothing about pay or experience (linkedin.py:124-143).
# - Indeed's job card JSON adds a salary snippet, job type and a remote marker,
#   but says nothing about experience either (indeed.py:163-181).
# - Naukri is the only one that publishes required experience (naukri.py:110-136).
# - Google Jobs is Search: pay and date are classified out of a card's visible
#   text when they appear at all, and there is no posting url (googlejobs.py:116-159).
CAPABILITIES = {
    'linkedin': Capability(
        filters=frozenset({'location', 'posted'}),
        publishes=frozenset({'posted', 'url', 'id'}),
    ),
    'indeed': Capability(
        filters=frozenset({'location', 'posted'}),
        publishes=frozenset({'salary', 'posted', 'url', 'id', 'employment_type', 'remote'}),
    ),
    'naukri': Capability(
        filters=frozenset({'location'}),
        publishes=frozenset({'experience', 'salary', 'posted', 'url', 'id'}),
    ),
    'googlejobs': Capability(
        filters=frozenset({'location'}),
        publishes=frozenset({'salary', 'posted', 'employment_type', 'via'}),
    ),
}

# A source we do not know is assumed to publish everything, so a missing field
# reads as the posting's silence rather than the board's - which is how every
# filter behaved before capabilities existed.
UNKNOWN = Capability(publishes=frozenset({'experience', 'salary', 'posted'}))


def capability(source: str | None) -> Capability:
    """What the named board does. Unknown sources get the permissive default."""
    return CAPABILITIES.get(source or '', UNKNOWN)
