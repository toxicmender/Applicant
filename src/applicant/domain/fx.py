"""Durable market FX snapshots; rates are quote units per one base unit."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import model_validator

from .contracts import Contract, CurrencyCode, Identity, Positive, Timestamp, VersionedContract


class FXFields(VersionedContract):
    snapshot_id: Identity
    base_currency: CurrencyCode
    source: Identity
    captured_at: Timestamp
    retrieved_at: Timestamp
    effective_date: date
    provenance: Literal['live', 'cache', 'static', 'identity']
    dataset_version: Identity | None = None
    rates: dict[CurrencyCode, Positive]

    @model_validator(mode='after')
    def consistent(self):
        if self.rates.get(self.base_currency) != Decimal(1):
            raise ValueError('base currency must have rate 1')
        if self.provenance == 'static' and self.dataset_version is None:
            raise ValueError('static rates require dataset version')
        if self.provenance == 'identity' and self.rates != {self.base_currency: Decimal(1)}:
            raise ValueError('identity snapshot may contain only its base currency')
        if self.effective_date > self.retrieved_at.date() or self.retrieved_at > self.captured_at:
            raise ValueError('FX observation chronology is inconsistent')
        return self


class FXSnapshot(FXFields, Contract):
    """Original snapshot format, retained for historical validation."""
