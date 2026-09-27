"""Kept so `from applicant.places import ...` goes on working; see `applicant.domain.places`."""

from .domain.places import (
    COUNTRIES,
    COUNTRY_OF_PLACE,
    WITHIN,
    countries_in,
    country_for,
    names,
    within,
)

__all__ = [
    'COUNTRIES',
    'COUNTRY_OF_PLACE',
    'WITHIN',
    'countries_in',
    'country_for',
    'names',
    'within',
]
