"""Immutable benchmark inputs, with source-specific wage cohorts."""

import base64
import hashlib
from datetime import date
from typing import Literal, Protocol

from pydantic import Field, model_validator

from .canonical import content_hash
from .contracts import Contract, CurrencyCode, Identity, Money, Positive, Timestamp


class BenchmarkQuery(Contract):
    kind: Literal['benchmark_query'] = 'benchmark_query'
    provider: Identity
    dataset: Identity
    filters: dict[str, tuple[Identity, ...]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def nonempty_filters(self):
        if any(
            not key.strip() or not values or len(set(values)) != len(values)
            for key, values in self.filters.items()
        ):
            raise ValueError('filters require nonblank keys and nonempty unique selections')
        return self


class BenchmarkResponse(Contract):
    source_url: Identity
    body_base64: str
    body_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')

    @property
    def body(self) -> bytes:
        return base64.b64decode(self.body_base64, validate=True)

    @property
    def response_id(self) -> str:
        return content_hash(self)

    @model_validator(mode='after')
    def exact_body(self):
        if hashlib.sha256(self.body).hexdigest() != self.body_sha256:
            raise ValueError('raw response digest does not match bytes')
        return self

    @classmethod
    def capture(cls, source_url: str, body: bytes):
        return cls(
            source_url=source_url,
            body_base64=base64.b64encode(body).decode('ascii'),
            body_sha256=hashlib.sha256(body).hexdigest(),
        )


class BenchmarkCohort(Contract):
    geography_system: Identity
    geography_code: Identity
    occupation_system: Identity
    occupation_code: Identity
    population: Identity
    industry_system: Identity
    industry_code: Identity
    working_time: Identity
    reference_start: date
    reference_end: date
    methodology: Identity
    wage_definition: Identity
    basis: Literal['gross', 'net', 'unknown']
    statistic: Literal['mean', 'p10', 'p25', 'median', 'p75', 'p90']
    dimensions: dict[str, Identity] = Field(default_factory=dict)

    @model_validator(mode='after')
    def chronology(self):
        if self.reference_end < self.reference_start:
            raise ValueError('cohort reference period is reversed')
        if any(not key.strip() for key in self.dimensions):
            raise ValueError('cohort dimension keys must not be blank')
        return self


class BenchmarkObservation(Contract):
    observation_id: Identity
    response_id: Identity
    cohort: BenchmarkCohort
    amount: Money | None
    currency: CurrencyCode
    period: Literal['annual', 'monthly', 'daily', 'hourly']
    status: Literal['available', 'suppressed', 'unavailable', 'censored'] = 'available'
    flags: tuple[Identity, ...] = ()
    notes: tuple[Identity, ...] = ()

    @model_validator(mode='after')
    def known_value(self):
        if self.status == 'available' and self.amount is None:
            raise ValueError('available observations require an amount')
        if self.status in ('suppressed', 'unavailable') and self.amount is not None:
            raise ValueError('missing observations cannot claim an amount')
        return self


class BenchmarkSnapshot(Contract):
    kind: Literal['benchmark_snapshot'] = 'benchmark_snapshot'
    snapshot_id: Identity
    query: BenchmarkQuery
    adapter_version: Identity
    retrieved_at: Timestamp
    released_at: Timestamp | None = None
    release_label: Identity | None = None
    responses: tuple[BenchmarkResponse, ...] = Field(min_length=1)
    observations: tuple[BenchmarkObservation, ...]

    @property
    def snapshot_hash(self) -> str:
        return content_hash(self.model_dump(exclude={'snapshot_id'}))

    @model_validator(mode='after')
    def provenance(self):
        ids = {r.response_id for r in self.responses}
        if len(ids) != len(self.responses):
            raise ValueError('duplicate raw response')
        if len({o.observation_id for o in self.observations}) != len(self.observations):
            raise ValueError('duplicate observation identity')
        if any(o.response_id not in ids for o in self.observations):
            raise ValueError('observation response is not in snapshot')
        if self.released_at is not None and self.released_at > self.retrieved_at:
            raise ValueError('release is after retrieval')
        return self


class BenchmarkSelection(Contract):
    job_id: Identity
    snapshot_id: Identity
    observation_id: Identity
    # This is a caller's explicit assertion, never inferred from a job title.
    expected_cohort: BenchmarkCohort
    annual_hours: Positive | None = None
    annual_days: Positive | None = None


class BenchmarkComparisonDocument(Contract):
    kind: Literal['benchmark_comparison'] = 'benchmark_comparison'
    selections: tuple[BenchmarkSelection, ...]

    @model_validator(mode='after')
    def unique_selections(self):
        ids = [(s.job_id, s.snapshot_id, s.observation_id) for s in self.selections]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate job/cohort selection')
        return self


class BenchmarkProvider(Protocol):
    """Acquisition adapters complete a whole query before returning a snapshot."""

    def discover(self) -> dict: ...
    def fetch(self, query: BenchmarkQuery) -> BenchmarkSnapshot: ...
