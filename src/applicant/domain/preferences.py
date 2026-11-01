"""Candidate preferences: empty target lists mean unrestricted."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .contracts import (
    Basis,
    Contract,
    CurrencyCode,
    Fraction,
    Identity,
    Money,
    Timestamp,
    VersionedContract,
    now,
)

DIMENSIONS = (
    'compensation',
    'remote_compatibility',
    'technical_fit',
    'company_culture',
    'career_growth',
    'stability',
)


def default_weights() -> dict[str, Decimal]:
    return dict(
        zip(DIMENSIONS, map(Decimal, ('0.25', '0.20', '0.20', '0.15', '0.10', '0.10')), strict=True)
    )


class ProfileFields(VersionedContract):
    profile_id: Identity
    profile_revision: int = Field(default=1, ge=1, strict=True)
    survey_version: Literal[1] = 1
    updated_at: Timestamp = Field(default_factory=now)
    experience_years: Money
    primary_discipline: Identity
    target_seniority: tuple[Literal['Junior', 'Mid', 'Senior', 'Staff', 'Principal'], ...] = ()
    core_technologies: tuple[Identity, ...] = ()
    preferred_regions: tuple[Identity, ...] = ()
    preferred_work_modes: tuple[Literal['Remote', 'Hybrid', 'Onsite'], ...] = ()
    timezone_overlap_tolerance: str | None = None
    minimum_base_salary: Money | None = None
    preferred_base_salary: Money | None = None
    salary_currency: CurrencyCode = 'EUR'
    salary_basis: Basis = 'gross'
    employment_preference: Literal['permanent_only', 'contract_only', 'either'] = 'permanent_only'
    weights: dict[str, Fraction] = Field(default_factory=default_weights)

    @model_validator(mode='after')
    def consistent(self):
        if set(self.weights) != set(DIMENSIONS):
            raise ValueError('weights must contain exactly the six supported dimensions')
        if abs(sum(self.weights.values()) - Decimal(1)) >= Decimal('0.000001'):
            raise ValueError('weights must sum to 1 within tolerance')
        if (
            self.minimum_base_salary is not None
            and self.preferred_base_salary is not None
            and self.minimum_base_salary > self.preferred_base_salary
        ):
            raise ValueError('minimum salary exceeds preferred salary')
        return self


class CandidateProfile(ProfileFields, Contract):
    """Original profile format, retained for historical validation."""
