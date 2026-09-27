"""The reviews clients' error names, kept as aliases of `applicant.errors`.

`ReviewsError` is `SourceError`, so `except ReviewsError` catches every failure
a reviews client raises - and, since it is the shared base, any other source's
too. See `applicant.errors` for the hierarchy.
"""

from ..errors import Blocked, NotFound, SourceError, Unparseable

ReviewsError = SourceError
CompanyNotFound = NotFound
ChallengeError = Blocked
ParseError = Unparseable

__all__ = ['ChallengeError', 'CompanyNotFound', 'ParseError', 'ReviewsError']
