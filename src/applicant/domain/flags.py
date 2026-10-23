"""The flags a posting can carry, and what each one means.

A flag says why a job survived a check it could not fully pass, or where one
came from. They are stored and exported as these exact strings - in
`job_listing.json` and in the `flags` column of `applied_jobs.csv` - so the
values here are a file format: renaming one breaks every log already written.

Naming them once means a typo is an import error rather than a filter that
silently never matches, and the README's tables can be checked against this
module.
"""

from __future__ import annotations

# -- the posting did not say ----------------------------------------------
# A board that publishes the field, and a posting that left it out.
SALARY_UNKNOWN = 'salary-unknown'
EXPERIENCE_UNKNOWN = 'experience-unknown'
DATE_UNKNOWN = 'date-unknown'

# -- the board never says -------------------------------------------------
# Not the posting's silence but the board's: it has no such field at all.
SALARY_UNPUBLISHED = 'salary-unpublished'
EXPERIENCE_UNPUBLISHED = 'experience-unpublished'
DATE_UNPUBLISHED = 'date-unpublished'

# -- could not be checked for another reason ------------------------------
LOCATION_UNVERIFIED = 'location-unverified'  # a place in no table we have
SALARY_CURRENCY_MISMATCH = 'salary-currency-mismatch'  # --salary-basis strict
SALARY_CURRENCY_ASSUMED = 'salary-currency-assumed'  # a bare number, no symbol

# -- how a converted salary was converted ---------------------------------
PPP_UNAVAILABLE = 'ppp-unavailable'  # fell back from PPP to a market rate
RATE_UNAVAILABLE = 'rate-unavailable'  # no market rate either
CURRENCY_UNKNOWN = 'currency-unknown'  # nothing to convert from or to

# -- where a posting's data came from -------------------------------------
EXPERIENCE_ENRICHED = 'experience-enriched'  # read off the posting itself

# `also-on-indeed`: the same job was on that board too, and this copy won
ALSO_ON = 'also-on-'

# the `<stem>-unknown` / `<stem>-unpublished` pairs, by the field they are about
UNKNOWN = {'salary': SALARY_UNKNOWN, 'experience': EXPERIENCE_UNKNOWN, 'posted': DATE_UNKNOWN}
UNPUBLISHED = {
    'salary': SALARY_UNPUBLISHED,
    'experience': EXPERIENCE_UNPUBLISHED,
    'posted': DATE_UNPUBLISHED,
}


def also_on(source: str) -> str:
    """'indeed' -> 'also-on-indeed'."""
    return ALSO_ON + source


def is_also_on(flag: str) -> bool:
    return flag.startswith(ALSO_ON)
