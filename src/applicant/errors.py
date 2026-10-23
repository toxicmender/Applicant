"""One error hierarchy for every source: job boards, review sites, financials.

Each product area used to grow its own - `JobsError`, `ReviewsError`,
`FinancialsError` - with the same kinds of failure under different names in
each. They are one hierarchy now, named for what went wrong rather than for
which area it went wrong in, so a caller handles "blocked", "not found" or
"the layout changed" once whichever source raised it:

    ApplicantError
    ├── SourceError          a source failed; `source` names it when known
    │   ├── Blocked          bot check, captcha, login wall, rate limit
    │   ├── NotFound         the company or posting does not exist
    │   ├── Unparseable      it loaded, but not in any shape we can read
    │   ├── AuthFailed       a key rejected, or a plan that lacks the data
    │   │   └── QuotaExhausted   out of credits: retrying cannot help
    │   └── Unreachable      the network, after the retries were spent
    ├── StoreError
    └── ConfigError

Before 0.2.0 each area had its own names for these - `JobsError`,
`ReviewsError`, `FinancialsError`, `ChallengeError`, `CompanyNotFound`,
`ParseError`, `AuthError` - kept for a while as aliases. They are gone; these
are the only names.
"""

from __future__ import annotations


class ApplicantError(Exception):
    """Base class for every failure this package raises on purpose."""


class SourceError(ApplicantError):
    """A source - a job board, review site or financials provider - failed.

    `source` is the short name (`'linkedin'`, `'crunchbase'`) where the raiser
    knows it. The message still reads on its own without it.
    """

    def __init__(self, message: str = '', *, source: str | None = None):
        super().__init__(message)
        self.source = source


class Blocked(SourceError):
    """A bot check, captcha, login wall or rate limit stopped the request."""


class NotFound(SourceError):
    """The name, slug, url or id does not resolve to anything."""


class Unparseable(SourceError):
    """The page or payload arrived, but none of the expected data was in it."""


class AuthFailed(SourceError):
    """An API key was rejected or expired, or the plan does not cover the data."""


class QuotaExhausted(AuthFailed):
    """The account is out of credits. Retrying cannot help until they renew."""


class Unreachable(SourceError):
    """The network failed, and went on failing through every retry."""


class StoreError(ApplicantError):
    """A file or database this package keeps could not be read or written."""


class ConfigError(ApplicantError):
    """The settings, flags or environment do not describe a runnable job."""
