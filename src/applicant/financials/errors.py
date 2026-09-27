class FinancialsError(Exception):
    """Base class for every failure raised by the financials clients."""


class CompanyNotFound(FinancialsError):
    """The name, slug, url or id does not resolve to a company."""


class AuthError(FinancialsError):
    """An API key was rejected, expired, out of credits, or the plan lacks the data."""


class ChallengeError(FinancialsError):
    """A bot check (Cloudflare interstitial, login wall) blocked the request."""


class ParseError(FinancialsError):
    """The page loaded but none of the expected data was found."""
