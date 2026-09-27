"""Kept so `from applicant.models import Job, JobsError` goes on working.

The model lives in `applicant.domain.job`. The error names are the job boards'
old ones, aliased to the shared hierarchy in `applicant.errors`.
"""

from .domain.job import EXPERIENCE, FRESHER, YEAR_LIMIT, Job, experience_from, parse_experience
from .errors import Blocked, SourceError

JobsError = SourceError
BlockedError = Blocked

__all__ = [
    'EXPERIENCE',
    'FRESHER',
    'YEAR_LIMIT',
    'BlockedError',
    'Job',
    'JobsError',
    'experience_from',
    'parse_experience',
]
