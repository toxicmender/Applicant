class ReviewsError(Exception):
    """Base class for every failure raised by the reviews clients."""


class CompanyNotFound(ReviewsError):
    """The company slug/url does not resolve to a reviews page."""


class ChallengeError(ReviewsError):
    """A bot check (Cloudflare interstitial, login wall) blocked the request."""


class ParseError(ReviewsError):
    """The page loaded but the expected data blob was missing or malformed."""
