"""Compensation facts and supplied normalization results; no salary algorithm."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Basis, Contract, CurrencyCode, Fraction, Identity, Money, Positive, Strategy


class NormalizedCompensation(Contract):
    original_text: str | None = None
    original_minimum: Money | None = None
    original_maximum: Money | None = None
    original_currency: CurrencyCode | None = None
    original_period: Literal['annual', 'monthly', 'weekly', 'daily', 'hourly'] | None = None
    original_basis: Basis | None = None
    status: Literal['available', 'unavailable', 'review_required'] = 'unavailable'
    annual_minimum: Money | None = None
    annual_maximum: Money | None = None
    normalized_currency: CurrencyCode | None = None
    normalized_basis: Basis | None = None
    representative_value: Money | None = None
    normalization_method: Strategy | None = None
    normalizer_version: Identity | None = None
    rounding_policy: Identity | None = None
    assumed_annual_hours: Positive | None = None
    assumed_annual_days: Positive | None = None
    fx_snapshot_id: Identity | None = None
    scoring_config_id: Identity | None = None
    evidence_quality: Fraction = Field(default=Decimal(0))
    warnings: tuple[str, ...] = ()

    @model_validator(mode='after')
    def consistent(self):
        for low, high in (
            (self.original_minimum, self.original_maximum),
            (self.annual_minimum, self.annual_maximum),
        ):
            if low is not None and high is not None and low > high:
                raise ValueError('minimum exceeds maximum')
        if self.representative_value is not None:
            if self.annual_minimum is not None and self.representative_value < self.annual_minimum:
                raise ValueError('representative value below minimum')
            if self.annual_maximum is not None and self.representative_value > self.annual_maximum:
                raise ValueError('representative value above maximum')
        normalized = any(
            value is not None
            for value in (self.annual_minimum, self.annual_maximum, self.representative_value)
        )
        if normalized or self.status == 'available':
            required = (
                self.original_currency,
                self.original_period,
                self.original_basis,
                self.normalized_currency,
                self.normalized_basis,
                self.normalizer_version,
                self.rounding_policy,
                self.normalization_method,
                self.fx_snapshot_id,
                self.scoring_config_id,
            )
            if not all(required) or not normalized or self.status != 'available':
                raise ValueError(
                    'available normalization requires units, policy and run references'
                )
            if self.original_basis != self.normalized_basis:
                raise ValueError('gross/net conversion is unsupported')
            if self.original_period == 'hourly' and self.assumed_annual_hours is None:
                raise ValueError('hourly normalization requires annual hours')
            if self.original_period == 'daily' and self.assumed_annual_days is None:
                raise ValueError('daily normalization requires annual days')
        return self
