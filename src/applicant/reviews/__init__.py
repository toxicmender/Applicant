"""Company rating clients for AmbitionBox and Glassdoor.

    from applicant.reviews import AmbitionBoxClient

    rating = AmbitionBoxClient().fetch('tcs', max_reviews=40)
    print(rating.overall_rating, rating.review_count)
    print(rating.reviews[0].pros, rating.reviews[0].cons)

Both clients expose the same fetch(company, max_reviews) -> CompanyRating, so
callers never have to branch on the source.
"""

from .ambitionbox import AmbitionBoxClient
from .errors import ChallengeError, CompanyNotFound, ParseError, ReviewsError
from .glassdoor import GlassdoorClient
from .models import CompanyRating, Review

__all__ = [
    'AmbitionBoxClient',
    'ChallengeError',
    'CompanyNotFound',
    'CompanyRating',
    'GlassdoorClient',
    'ParseError',
    'Review',
    'ReviewsError',
]
