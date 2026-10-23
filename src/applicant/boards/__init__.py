"""One module per job board, each returning `applicant.domain.job.Job`.

Clients are imported lazily by `applicant.search.Jobs` so that a board needing a
browser costs nothing until it is actually used. What each board can *do*, on the
other hand, is needed before one is chosen - by the facade, to know which filters
it may skip, and by `applicant.filters`, to know whose silence a missing field is.
So capabilities live here as plain data, importable without pulling in httpx or
Playwright. Each board class carries its own entry as `capability`.

The declarations are checked rather than documented: `tests/test_boards.py`
parses each board's fixtures and asserts that every field a board claims to
publish turns up, and that nothing it publishes is left undeclared.
"""

from __future__ import annotations

from ..domain.capability import UNKNOWN, Capability, Field

__all__ = ['CAPABILITIES', 'UNKNOWN', 'Capability', 'Field', 'capability']

CAPABILITIES = {
    # the guest card: title, company, location and date; nothing on pay or experience
    'linkedin': Capability(
        filters=frozenset({Field.LOCATION, Field.POSTED}),
        publishes=frozenset({Field.POSTED, Field.URL, Field.ID}),
    ),
    # the job card JSON adds a salary snippet, job type and a remote marker
    'indeed': Capability(
        filters=frozenset({Field.LOCATION, Field.POSTED}),
        publishes=frozenset(
            {Field.SALARY, Field.POSTED, Field.URL, Field.ID, Field.EMPLOYMENT_TYPE, Field.REMOTE}
        ),
    ),
    # the only board that publishes required experience
    'naukri': Capability(
        filters=frozenset({Field.LOCATION}),
        publishes=frozenset({Field.EXPERIENCE, Field.SALARY, Field.POSTED, Field.URL, Field.ID}),
    ),
    # Google Search: pay and date are classified out of a card's visible text
    # when they appear at all, and there is no posting url
    'googlejobs': Capability(
        filters=frozenset({Field.LOCATION}),
        publishes=frozenset({Field.SALARY, Field.POSTED, Field.EMPLOYMENT_TYPE, Field.VIA}),
    ),
}


def capability(source: str | None) -> Capability:
    """What the named board does. Unknown sources get the permissive default."""
    return CAPABILITIES.get(source or '', UNKNOWN)
