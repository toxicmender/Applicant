"""Typed, captured evidence for the ranking engine; no acquisition or inference."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from iso3166 import countries_by_alpha2
from pydantic import AfterValidator, Field, StringConstraints, field_validator, model_validator

from .contracts import Basis, Contract, CurrencyCode, Identity, Money, Timestamp

WorkMode = Literal['Remote', 'Hybrid', 'Onsite']
Quality = Literal['explicit', 'verified', 'structured', 'inferred', 'fallback', 'unknown']


def country(value: str) -> str:
    if value not in countries_by_alpha2:
        raise ValueError('unknown ISO 3166 alpha-2 country')
    return value


CountryCode = Annotated[str, StringConstraints(pattern=r'^[A-Z]{2}$'), AfterValidator(country)]


class FactSource(Contract):
    evidence_id: Identity
    source: Identity
    observed_at: Timestamp
    extractor_version: Identity
    quality: Quality
    excerpt: Identity


class CompensationFacts(Contract):
    source: FactSource
    original_text: Identity
    minimum: Money | None = None
    maximum: Money | None = None
    shape: Literal['exact', 'range', 'lower_only', 'upper_only', 'unknown'] = 'unknown'
    currency: CurrencyCode | None = None
    period: Literal['annual', 'monthly', 'weekly', 'daily', 'hourly'] | None = None
    basis: Basis | None = None
    component: Literal['base', 'total', 'unknown'] = 'unknown'
    # Different tiers are independent offers, never merged into an artificial range.
    tier: Identity | None = None

    @model_validator(mode='after')
    def bounds(self):
        low, high = self.minimum, self.maximum
        if low is not None and high is not None and low > high:
            raise ValueError('minimum exceeds maximum')
        valid = {
            'exact': low is not None and low == high,
            'range': low is not None and high is not None,
            'lower_only': low is not None and high is None,
            'upper_only': low is None and high is not None,
            'unknown': low is None and high is None,
        }
        if not valid[self.shape]:
            raise ValueError('compensation shape does not match bounds')
        return self


class WorkRoute(Contract):
    route_id: Identity
    source: FactSource
    mode: WorkMode
    remote_scope: Literal['worldwide', 'country_list', 'unknown'] = 'unknown'
    allowed_countries: tuple[CountryCode, ...] = ()
    country_registry: Literal['iso3166-3.0.0'] = 'iso3166-3.0.0'
    residual_restrictions: tuple[Identity, ...] = ()

    @field_validator('allowed_countries')
    @classmethod
    def ordered_countries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(values)))

    @model_validator(mode='after')
    def scope(self):
        if self.remote_scope == 'country_list' and not self.allowed_countries:
            raise ValueError('country-list route requires countries')
        if self.remote_scope != 'country_list' and self.allowed_countries:
            raise ValueError('countries require country-list scope')
        if self.mode == 'Onsite' and self.remote_scope != 'unknown':
            raise ValueError('onsite routes cannot assert remote permission')
        return self


class TechnologyRequirements(Contract):
    source: FactSource
    technologies: tuple[Identity, ...] = ()
    complete: bool = False


class EmploymentOptions(Contract):
    source: FactSource
    arrangements: tuple[Literal['permanent', 'contract', 'other'], ...] = ()
    complete: bool = False


class CategoryRating(Contract):
    source: FactSource
    company_id: Identity
    company_url: Identity
    category: Literal[
        'company_culture',
        'culture_and_values',
        'career_growth',
        'career_opportunities',
        'job_security',
    ]
    value: Decimal = Field(ge=1, le=5, allow_inf_nan=False)
    review_count: int | None = Field(default=None, ge=1, strict=True)
    scale: Literal['one-to-five'] = 'one-to-five'


class RankingFacts(Contract):
    """Embed model_dump(mode='json') in Evidence.payload; the outer hash covers it."""

    kind: Literal['ranking_facts'] = 'ranking_facts'
    compensation: tuple[CompensationFacts, ...] = ()
    routes: tuple[WorkRoute, ...] = ()
    routes_complete: bool = False
    employment: tuple[EmploymentOptions, ...] = ()
    technologies: tuple[TechnologyRequirements, ...] = ()
    ratings: tuple[CategoryRating, ...] = ()

    @model_validator(mode='after')
    def unique_routes(self):
        if len({route.route_id for route in self.routes}) != len(self.routes):
            raise ValueError('duplicate route ID')
        return self
