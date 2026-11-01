"""Shared offline validation policies for versioned ranking contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from iso4217 import Currency
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints

from .canonical import utc

REGISTRY_VERSION = 'iso4217-1.16.20260101'


def currency(code: str) -> str:
    if code not in Currency.__members__:
        raise ValueError(f'unknown ISO 4217 currency: {code}')
    return code


Identity = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r'^[A-Z]{3}$'), AfterValidator(currency)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Fraction = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
Timestamp = Annotated[datetime, AfterValidator(utc)]
Basis = Literal['gross', 'net']
Strategy = Literal['minimum', 'midpoint', 'maximum']


def now() -> datetime:
    return datetime.now(timezone.utc)


class VersionedContract(BaseModel):
    # Repositories serialize/revalidate at ingress: frozen does not freeze nested
    # dictionaries, and model_copy(update=...) bypasses validation.
    model_config = ConfigDict(frozen=True, extra='forbid', validate_default=True)
    currency_registry: Literal['iso4217-1.16.20260101'] = REGISTRY_VERSION


class Contract(VersionedContract):
    schema_version: Literal[1] = 1
