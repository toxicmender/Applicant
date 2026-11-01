"""Versioned inputs and supplied results for ranking history, without scoring."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .canonical import content_hash
from .compensation import NormalizedCompensation
from .contracts import Contract, Fraction, Identity, Positive, Strategy, Timestamp, now
from .dedupe import key
from .job import Job
from .preferences import DIMENSIONS


class ScoringConfiguration(Contract):
    scoring_config_id: Identity = Field(default_factory=lambda: str(uuid4()))
    config_version: Identity = '1.0.0'
    created_at: Timestamp = Field(default_factory=now)
    representative_salary_strategy: Strategy = 'midpoint'
    annual_hours: Positive = Decimal(2080)
    annual_days: Positive = Decimal(260)
    normalizer_version: Identity = '1'
    rounding_policy: Literal['decimal-half-even-2'] = 'decimal-half-even-2'
    tie_breaking_policy: Literal['score-desc-job-id-asc'] = 'score-desc-job-id-asc'
    evidence_weights: dict[str, Fraction] = Field(
        default_factory=lambda: {
            'explicit': Decimal(1),
            'verified': Decimal('.9'),
            'structured': Decimal('.8'),
            'inferred': Decimal('.5'),
            'fallback': Decimal('.5'),
            'unknown': Decimal(0),
        }
    )

    @model_validator(mode='after')
    def consistent(self):
        if set(self.evidence_weights) != {
            'explicit',
            'verified',
            'structured',
            'inferred',
            'fallback',
            'unknown',
        }:
            raise ValueError('unsupported evidence categories')
        return self

    @property
    def config_hash(self) -> str:
        return content_hash(self.model_dump(exclude={'scoring_config_id', 'created_at'}))


class Evidence(Contract):
    source: Identity
    observed_at: Timestamp
    extractor_version: Identity
    payload: dict[str, Any]


class JobVersion(Contract):
    job_version_id: Identity = Field(default_factory=lambda: str(uuid4()))
    captured_at: Timestamp = Field(default_factory=now)
    identity_policy: Literal[1] = 1
    hash_policy: Literal[1] = 1
    parser_version: Identity
    job_data: dict[str, Any]
    description: str | None = None
    evidence: tuple[Evidence, ...] = ()

    @model_validator(mode='after')
    def consistent(self):
        job = Job.from_dict(self.job_data)
        if not job.source.strip() or not job.title.strip():
            raise ValueError('job source and title must not be blank')
        # Also rejects non-JSON and non-finite evidence at the contract boundary.
        content_hash(self.model_dump())
        return self

    @property
    def job_id(self) -> str:
        return content_hash({'identity_policy': self.identity_policy, 'key': key(self.job_data)})

    @property
    def content_hash(self) -> str:
        return content_hash(self.model_dump(exclude={'job_version_id', 'captured_at'}))

    @property
    def ranking_hash(self) -> str:
        fields = (
            'title',
            'company',
            'location',
            'salary',
            'remote',
            'employment_type',
            'experience_text',
            'experience_min',
            'experience_max',
        )
        return content_hash(
            {
                'hash_policy': self.hash_policy,
                'parser_version': self.parser_version,
                'job': {name: self.job_data.get(name) for name in fields},
                'description': self.description,
                'evidence': self.evidence,
            }
        )

    @classmethod
    def capture(
        cls,
        job: Job,
        *,
        parser_version: str,
        description: str | None = None,
        evidence: tuple[Evidence, ...] = (),
    ) -> JobVersion:
        # Normalize irrelevant boundary whitespace via the existing Job model;
        # transient filter flags must not make a new content version.
        data = Job.model_validate(job.model_dump()).model_dump(exclude={'flags'})
        return cls(
            parser_version=parser_version, job_data=data, description=description, evidence=evidence
        )


class JobRanking(Contract):
    job_id: Identity
    job_version_id: Identity
    eligibility: Literal['eligible', 'review_required', 'excluded']
    reasons: tuple[str, ...] = ()
    overall_score: Decimal | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    confidence: Fraction | None = None
    dimension_scores: dict[str, Decimal] = Field(default_factory=dict)
    compensation: NormalizedCompensation | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.dimension_scores and set(self.dimension_scores) != set(DIMENSIONS):
            raise ValueError('dimension scores must contain all six dimensions')
        if any(
            not value.is_finite() or not 0 <= value <= 100
            for value in self.dimension_scores.values()
        ):
            raise ValueError('dimension scores must be finite and between 0 and 100')
        return self


class RankingRun(Contract):
    ranking_run_id: Identity = Field(default_factory=lambda: str(uuid4()))
    profile_id: Identity
    profile_revision: int = Field(ge=1, strict=True)
    fx_snapshot_id: Identity
    scoring_config_id: Identity
    algorithm_version: Identity
    build_identity: Identity
    lockfile_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    calculated_at: Timestamp = Field(default_factory=now)
    job_version_ids: tuple[Identity, ...] = ()
    results: tuple[JobRanking, ...] = ()

    @model_validator(mode='after')
    def consistent(self):
        if len(set(self.job_version_ids)) != len(self.job_version_ids):
            raise ValueError('duplicate job version in run')
        if len({result.job_version_id for result in self.results}) != len(self.results):
            raise ValueError('duplicate result')
        if any(result.job_version_id not in self.job_version_ids for result in self.results):
            raise ValueError('result is not in run membership')
        for result in self.results:
            pay = result.compensation
            if (
                pay is not None
                and pay.status == 'available'
                and (
                    pay.fx_snapshot_id != self.fx_snapshot_id
                    or pay.scoring_config_id != self.scoring_config_id
                )
            ):
                raise ValueError('normalization references different run inputs')
        return self
