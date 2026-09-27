"""The financials clients' error names, kept as aliases of `applicant.errors`.

`FinancialsError` is `SourceError`, so `except FinancialsError` catches every
failure a financials client raises - and, since it is the shared base, any
other source's too. `QuotaExhausted` is an `AuthError`, so code that already
handles a refused key handles running out of credits as well. See
`applicant.errors` for the hierarchy.
"""

from ..errors import AuthFailed, Blocked, NotFound, QuotaExhausted, SourceError, Unparseable

FinancialsError = SourceError
CompanyNotFound = NotFound
AuthError = AuthFailed
ChallengeError = Blocked
ParseError = Unparseable

__all__ = [
    'AuthError',
    'ChallengeError',
    'CompanyNotFound',
    'FinancialsError',
    'ParseError',
    'QuotaExhausted',
]
