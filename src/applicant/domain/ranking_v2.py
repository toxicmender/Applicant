"""Version 2 engine contracts; version 1 history remains independently readable."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .canonical import content_hash
from .compensation import NormalizedCompensation
from .contracts import Fraction, Identity, Positive, Strategy, Timestamp, VersionedContract, now
from .fx import FXFields
from .preferences import DIMENSIONS, CandidateProfile, ProfileFields
from .ranking import ScoringConfiguration
from .ranking_facts import CountryCode, WorkMode

Score = Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]


class EngineContract(VersionedContract):
    schema_version: Literal[2] = 2


class CandidateProfileV2(ProfileFields, EngineContract):
    work_country: CountryCode | None = None
    country_registry: Literal['iso3166-3.0.0'] = 'iso3166-3.0.0'
    required_work_modes: tuple[WorkMode, ...] = ()


def upgrade_profile(profile: CandidateProfile, *, updated_at: Timestamp) -> CandidateProfileV2:
    """Prepare a new revision; persistence still requires optimistic revision checking."""
    data = profile.model_dump(exclude={'schema_version', 'updated_at'})
    data['profile_revision'] = profile.profile_revision + 1
    return CandidateProfileV2(**data, updated_at=updated_at)


class ScoringConfigurationV2(EngineContract):
    scoring_config_id: Identity = Field(default_factory=lambda: str(uuid4()))
    created_at: Timestamp = Field(default_factory=now)
    config_version: Literal['2.0.0'] = '2.0.0'
    algorithm_version: Literal['ranking-1'] = 'ranking-1'
    representative_salary_strategy: Strategy = 'midpoint'
    annual_hours: Positive = Decimal(2080)
    annual_days: Positive = Decimal(260)
    annual_weeks: Positive = Decimal(52)
    annual_months: Positive = Decimal(12)
    normalizer_version: Literal['2'] = '2'
    arithmetic_policy: Literal['decimal34-half-even-v1'] = 'decimal34-half-even-v1'
    rounding_policy: Literal['decimal-half-even-2'] = 'decimal-half-even-2'
    score_quantum: Literal['0.01'] = '0.01'
    confidence_quantum: Literal['0.0001'] = '0.0001'
    tie_breaking_policy: Literal['score-desc-job-id-asc'] = 'score-desc-job-id-asc'
    scoring_policy: Literal['six-dimensions-v1'] = 'six-dimensions-v1'
    eligibility_policy: Literal['routes-and-floor-v1'] = 'routes-and-floor-v1'
    aggregation_policy: Literal['known-only-with-coverage-v1'] = 'known-only-with-coverage-v1'
    fx_policy: Literal['ecb-cache-static-identity-v1'] = 'ecb-cache-static-identity-v1'
    fx_max_age_days: int = Field(default=7, ge=0, strict=True)
    allow_static_fx: bool = False
    review_max_age_days: int = Field(default=180, ge=0, strict=True)
    review_count_saturation: int = Field(default=20, ge=1, strict=True)
    missing_review_count_factor: Fraction = Decimal('.5')
    market_fx_quality: Fraction = Decimal('.9')
    static_fx_quality: Fraction = Decimal('.5')
    evidence_weights: dict[str, Fraction] = Field(
        default_factory=lambda: ScoringConfiguration().evidence_weights.copy()
    )
    technology_aliases: dict[Identity, Identity] = Field(
        default_factory=lambda: {'py': 'python', 'js': 'javascript'}
    )
    source_priority: tuple[Identity, ...] = ('glassdoor', 'ambitionbox')
    category_dimensions: dict[str, str] = Field(
        default_factory=lambda: {
            'company_culture': 'company_culture',
            'culture_and_values': 'company_culture',
            'career_growth': 'career_growth',
            'career_opportunities': 'career_growth',
            'job_security': 'stability',
        }
    )

    @model_validator(mode='after')
    def policies(self):
        if (
            set(self.evidence_weights)
            != {'explicit', 'verified', 'structured', 'inferred', 'fallback', 'unknown'}
            or self.evidence_weights['unknown'] != 0
        ):
            raise ValueError('evidence weights require supported categories and unknown=0')
        if not self.source_priority or len(set(self.source_priority)) != len(self.source_priority):
            raise ValueError('source priority must be nonempty and unique')
        if any(
            value not in {'company_culture', 'career_growth', 'stability'}
            for value in self.category_dimensions.values()
        ):
            raise ValueError('unsupported review dimension')
        for alias, target in self.technology_aliases.items():
            if alias != alias.casefold().strip() or target != target.casefold().strip():
                raise ValueError('technology aliases must be canonical lowercase tokens')
            if target in self.technology_aliases and target != alias:
                raise ValueError('technology alias chains are unsupported')
        return self

    @property
    def config_hash(self) -> str:
        return content_hash(self.model_dump(exclude={'scoring_config_id', 'created_at'}))


def upgrade_config(
    config: ScoringConfiguration, *, created_at: Timestamp
) -> ScoringConfigurationV2:
    """Copy supported normalization settings into a newly identified engine policy."""
    return ScoringConfigurationV2(
        created_at=created_at,
        representative_salary_strategy=config.representative_salary_strategy,
        annual_hours=config.annual_hours,
        annual_days=config.annual_days,
        evidence_weights=config.evidence_weights.copy(),
    )


class FXSnapshotV2(FXFields, EngineContract):
    request_url: Identity | None = None
    provider: Identity | None = None
    raw_response: str | None = None
    response_digest: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    parent_snapshot_id: Identity | None = None

    @model_validator(mode='after')
    def acquisition(self):
        if (self.raw_response is None) != (self.response_digest is None):
            raise ValueError('response and digest must be supplied together')
        if self.raw_response is not None:
            digest = hashlib.sha256(self.raw_response.encode('utf-8')).hexdigest()
            if digest != self.response_digest:
                raise ValueError('FX response digest mismatch')
        if self.provenance == 'live' and (
            self.raw_response is None or self.request_url is None or self.provider is None
        ):
            raise ValueError('live acquisition requires request, provider and response')
        if self.parent_snapshot_id == self.snapshot_id:
            raise ValueError('FX snapshot cannot be its own parent')
        return self


class ConflictAssertion(EngineContract):
    evidence_id: Identity
    source: Identity
    observed_at: Timestamp
    normalized_value: Identity
    excerpt: Identity


class EvidenceConflict(EngineContract):
    code: Literal['conflicting_evidence'] = 'conflicting_evidence'
    criterion: Identity
    route_id: Identity | None = None
    assertions: tuple[ConflictAssertion, ...] = Field(min_length=2)
    explanation: Identity
    needs_verification: Identity

    @model_validator(mode='after')
    def disagreement(self):
        if len({item.evidence_id for item in self.assertions}) != len(self.assertions):
            raise ValueError('conflict assertions must reference distinct evidence')
        if len({item.normalized_value for item in self.assertions}) < 2:
            raise ValueError('conflict requires disagreeing values')
        return self


class CriterionResult(EngineContract):
    criterion: Identity
    status: Literal['pass', 'fail', 'unknown', 'not_applicable']
    reason_code: Identity
    explanation: Identity
    evidence_ids: tuple[Identity, ...] = ()
    conflicts: tuple[EvidenceConflict, ...] = ()

    @model_validator(mode='after')
    def unresolved(self):
        if self.conflicts and self.status != 'unknown':
            raise ValueError('unresolved conflict must be unknown')
        if any(item.criterion != self.criterion for item in self.conflicts):
            raise ValueError('conflict belongs to a different criterion')
        return self


class EligibilityDecision(EngineContract):
    criteria: tuple[CriterionResult, ...]

    @model_validator(mode='after')
    def unique_criteria(self):
        if not self.criteria or len({c.criterion for c in self.criteria}) != len(self.criteria):
            raise ValueError('criteria must be nonempty and unique')
        return self

    @property
    def status(self) -> Literal['eligible', 'review_required', 'excluded']:
        if any(item.status == 'fail' for item in self.criteria):
            return 'excluded'
        if any(item.status == 'unknown' for item in self.criteria):
            return 'review_required'
        return 'eligible'


class DimensionResult(EngineContract):
    dimension: Identity
    status: Literal['known', 'unknown', 'not_applicable', 'disabled', 'not_evaluated']
    score: Score | None = None
    quality: Fraction = Decimal(0)
    reason_codes: tuple[Identity, ...] = ()
    evidence_ids: tuple[Identity, ...] = ()
    calculation_terms: dict[str, Decimal] = Field(default_factory=dict)
    weighted_contribution: Score | None = None

    @model_validator(mode='after')
    def known_score(self):
        if self.dimension not in DIMENSIONS:
            raise ValueError('unsupported dimension')
        if (self.status == 'known') != (self.score is not None):
            raise ValueError('only known dimensions have scores')
        if self.status == 'known' and self.quality == 0:
            raise ValueError('known score requires positive evidence quality')
        if self.status != 'known' and (self.quality != 0 or self.weighted_contribution is not None):
            raise ValueError('unscored dimensions cannot claim support or contribution')
        if any(not value.is_finite() for value in self.calculation_terms.values()):
            raise ValueError('calculation terms must be finite')
        return self


class JobRankingV2(EngineContract):
    job_id: Identity
    job_version_id: Identity
    decision: EligibilityDecision
    dimensions: tuple[DimensionResult, ...]
    overall_score: Score | None = None
    confidence: Fraction | None = None
    coverage: Fraction | None = None
    compensation: NormalizedCompensation | None = None

    @property
    def eligibility(self) -> Literal['eligible', 'review_required', 'excluded']:
        return self.decision.status

    @model_validator(mode='after')
    def result(self):
        if len(self.dimensions) != len(DIMENSIONS) or {d.dimension for d in self.dimensions} != set(
            DIMENSIONS
        ):
            raise ValueError('result requires each of the six dimensions exactly once')
        if self.eligibility == 'excluded':
            if any(v is not None for v in (self.overall_score, self.confidence, self.coverage)):
                raise ValueError('excluded jobs cannot have score or confidence')
            if any(d.status != 'not_evaluated' for d in self.dimensions):
                raise ValueError('excluded dimensions must be not evaluated')
        else:
            known = any(d.status == 'known' for d in self.dimensions)
            if known != (self.overall_score is not None):
                raise ValueError('overall score requires known dimensions')
            if self.confidence is None or self.coverage is None or self.confidence > self.coverage:
                raise ValueError('confidence and coverage required, confidence <= coverage')
            if not known and (self.confidence != 0 or self.coverage != 0):
                raise ValueError('no known dimensions means zero evidence coverage')
            if any(d.status == 'not_evaluated' for d in self.dimensions):
                raise ValueError('only excluded jobs may skip scoring')
        return self


class RankingRunFields(VersionedContract):
    ranking_run_id: Identity = Field(default_factory=lambda: str(uuid4()))
    profile_id: Identity
    profile_revision: int = Field(ge=1, strict=True)
    fx_snapshot_id: Identity
    scoring_config_id: Identity
    algorithm_version: Literal['ranking-1'] = 'ranking-1'
    build_identity: Identity
    lockfile_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    calculated_at: Timestamp
    job_version_ids: tuple[Identity, ...] = ()
    results: tuple[JobRankingV2, ...] = ()
    resolution_diagnostics: tuple[Identity, ...] = ()

    @model_validator(mode='after')
    def complete(self):
        if len(set(self.job_version_ids)) != len(self.job_version_ids):
            raise ValueError('duplicate input version')
        if tuple(r.job_version_id for r in self.results) != self.job_version_ids:
            raise ValueError('engine results must cover every input in input order')
        if len({r.job_id for r in self.results}) != len(self.results):
            raise ValueError('duplicate job identity')
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


class RankingRunV2(RankingRunFields, EngineContract):
    """Original complete engine-run contract, preserved for historical reads."""


class RankingRunV3(RankingRunFields):
    """Freeze per-job run choices without changing immutable posting content."""

    schema_version: Literal[3] = 3
    selected_tiers: dict[Identity, Identity] = Field(default_factory=dict)
    engine_policy: Literal['frozen-inputs-v1'] = 'frozen-inputs-v1'

    @model_validator(mode='after')
    def selections(self):
        if not self.selected_tiers.keys() <= {result.job_id for result in self.results}:
            raise ValueError('salary tier selection is not a run input')
        return self
