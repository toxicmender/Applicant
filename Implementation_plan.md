# Applicant — Architecture & Implementation Plan

> Reviewed against repository commit `8512b42` on 2026-11-02.
> This draft needs integration and integrity corrections before implementation.
> See [repository evaluation and revised semantic commit plan](docs/implementation-plan-review.md).
> That review supersedes conflicting layout, migration, contract, and sequencing
> details below. The remaining text preserves the original proposal for reference.
> Milestone 1 is implemented. The [Milestone 2 implementation plan](docs/milestone-2-plan.md)
> records the delivered engine work, researched policies, compatible schema
> evolution, and acceptance criteria. The [Milestone 3 review and implementation
> plan](docs/milestone-3-plan.md), reviewed against `d375842`, records delivered
> survey/CLI work and supersedes the command sketch below. See the final
> [Milestone 3 coverage audit](docs/milestone-3-coverage.md) for local validation
> and remaining hosted gates.

Project: toxicmender/Applicant
Document: Implementation Plan and Technical Specification
Status: Milestones 1–3 implemented and validated locally; hosted CI remains unverified
Primary language: Python
Package manager: uv
Data validation: Pydantic
Persistence: SQLite
Initial implementation scope: Core data contracts and persistence layer

1. Project Overview
1.1 Objective

Applicant is a job discovery and application management application that will evolve to include a personalized survey, candidate preference management, opportunity eligibility filtering and explainable job ranking.

The survey and ranking system should help engineering professionals evaluate opportunities across:

Compensation and total rewards.
Remote-work compatibility.
Technical alignment.
Company culture.
Career growth.
Company stability.

The system should support international opportunities, particularly European remote roles, US remote roles and local technology hubs such as Bengaluru, Hyderabad and Gurgaon/NCR.

The ranking system must prioritize transparency and reproducibility over opaque recommendation scores.

1.2 Core architectural principle

Every ranking must be reproducible using five independently identifiable inputs:

Candidate profile revision.
Job content version.
FX snapshot.
Ranking algorithm version.
Scoring configuration.

These inputs must be preserved independently of subsequent changes to candidate preferences, job listings, exchange rates or ranking algorithms.

1.3 Engineering principles

The implementation will follow these principles:

Preserve existing Applicant discovery and application workflows.
Keep candidate preferences separate from scraped job records.
Use versioned Pydantic models.
Store historical profile revisions.
Normalize compensation before eligibility and scoring.
Preserve original compensation information.
Snapshot FX rates used by ranking runs.
Preserve immutable job versions.
Use deterministic scoring inputs.
Separate evidence quality from candidate-job compatibility.
Keep historical ranking results reproducible.
Support offline operation using cached reference data.
Make database migrations explicit and testable.
2. Overall Architecture
2.1 High-level architecture
mermaid
flowchart TD
    A[Candidate Survey / JSON Configuration] --> B[Pydantic Validation]
    B --> C[Candidate Profile Repository]
    C --> D[Versioned Candidate Profiles]

    E[Existing Job Database] --> F[Job Normalization]
    F --> G[Job Version Repository]

    H[FX Reference Data] --> I[FX Snapshot Repository]

    J[Scoring Configuration] --> K[Scoring Config Repository]

    D --> L[Ranking Run Service]
    G --> L
    I --> L
    K --> L

    L --> M[Immutable Ranking Runs]
    M --> N[Future Eligibility Engine]
    N --> O[Future Scoring Engine]
    O --> P[Future Confidence Engine]
    P --> Q[Ranked Job Results]
    Q --> R[CLI / Export / Explainability]


The diagram illustrates the eventual architecture. Milestone 1 implements the data contracts and persistence components, not the eligibility or scoring engines.

2.2 Architectural boundaries
Component	Responsibility
Preferences	Candidate profile models and validation
Survey	Questionnaire definitions and future survey interaction
Ranking	Compensation, FX, scoring configuration and future ranking logic
Storage	SQLite connections, migrations and persistence
Repositories	Database operations and transactional consistency
CLI	User-facing commands
Existing discovery	Job collection and ingestion

The storage layer must not contain ranking business logic.

The ranking layer must not directly execute arbitrary SQL.

Repositories should own persistence operations, while services coordinate operations across repositories.

3. Project Structure

The following is the proposed target structure. Before implementation, reconcile it with the repository's existing package layout and reuse existing database, CLI and job models where appropriate.

text
Applicant/
│
├── pyproject.toml
├── uv.lock
├── README.md
├── IMPLEMENTATION_PLAN.md
│
├── src/
│   └── applicant/
│       │
│       ├── __init__.py
│       ├── main.py
│       │
│       ├── preferences/
│       │   ├── __init__.py
│       │   ├── models.py
│       │   ├── repository.py
│       │   └── service.py
│       │
│       ├── survey/
│       │   ├── __init__.py
│       │   ├── questions.py
│       │   └── schema.py
│       │
│       ├── ranking/
│       │   ├── __init__.py
│       │   ├── config_models.py
│       │   ├── normalization.py
│       │   ├── fx_models.py
│       │   ├── eligibility.py
│       │   ├── scoring.py
│       │   ├── confidence.py
│       │   ├── fingerprint.py
│       │   └── service.py
│       │
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── database.py
│       │   ├── connection.py
│       │   │
│       │   ├── migrations/
│       │   │   ├── __init__.py
│       │   │   └── v001_initial_schema.sql
│       │   │
│       │   └── repositories/
│       │       ├── __init__.py
│       │       ├── profiles.py
│       │       ├── fx_snapshots.py
│       │       ├── scoring_configs.py
│       │       ├── job_versions.py
│       │       └── ranking_runs.py
│       │
│       └── ...
│
└── tests/
    ├── preferences/
    │   └── test_models.py
    │
    ├── ranking/
    │   ├── test_config_models.py
    │   ├── test_normalization_models.py
    │   └── test_fx_models.py
    │
    └── storage/
        ├── test_migrations.py
        ├── test_profiles.py
        ├── test_fx_snapshots.py
        ├── test_scoring_configs.py
        ├── test_job_versions.py
        └── test_ranking_runs.py


Important: eligibility.py, scoring.py, confidence.py and the future survey interaction components are architectural placeholders. They are not part of Milestone 1 implementation.

4. Five Immutable Ranking Inputs

The eventual ranking system must preserve the following inputs.

Input	Storage	Purpose
Candidate profile revision	candidate_profiles	Captures candidate preferences
Job content version	job_versions	Captures the exact job information
FX snapshot	fx_snapshots	Captures exchange rates
Algorithm version	ranking_runs	Identifies ranking implementation
Scoring configuration	scoring_configs	Captures scoring parameters

A ranking run references the exact candidate profile revision, FX snapshot and scoring configuration.

Each individual job ranking references its exact job version.

This avoids coupling ranking history to mutable current-state records.

4.1 Reproducibility requirements

A historical ranking must remain unchanged when:

Candidate preferences change.
A new job description is scraped.
Salary information is updated.
Exchange rates change.
The scoring configuration changes.
The ranking algorithm is upgraded.

A subsequent ranking run creates a new record rather than overwriting the previous result.

5. Milestone 1 — Core Data Contracts
5.1 CandidateProfile

File: src/applicant/preferences/models.py

The candidate profile represents the candidate's professional background, geographical preferences, compensation expectations and ranking weights.

python
from datetime import datetime, timezone
from math import isfinite
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


SUPPORTED_DIMENSIONS = {
    "compensation",
    "remote_compatibility",
    "technical_fit",
    "company_culture",
    "career_growth",
    "stability",
}


class CandidateProfile(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    # Identity and versioning
    profile_id: str
    profile_revision: int = Field(default=1, ge=1)
    schema_version: int = Field(default=1, ge=1)
    survey_version: int = Field(default=1, ge=1)

    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # Professional profile
    experience_years: float = Field(ge=0)
    primary_discipline: str

    target_seniority: list[
        Literal[
            "Junior",
            "Mid",
            "Senior",
            "Staff",
            "Principal",
        ]
    ]

    core_technologies: list[str] = Field(
        default_factory=list
    )

    # Geography and work arrangements
    preferred_regions: list[str] = Field(
        default_factory=list
    )

    preferred_work_modes: list[
        Literal["Remote", "Hybrid", "Onsite"]
    ]

    timezone_overlap_tolerance: str | None = None

    # Compensation expectations
    minimum_base_salary: float | None = Field(
        default=None,
        ge=0,
    )

    preferred_base_salary: float | None = Field(
        default=None,
        ge=0,
    )

    salary_currency: str = Field(
        default="EUR",
        pattern=r"^[A-Z]{3}$",
    )

    employment_preference: Literal[
        "permanent_only",
        "contract_only",
        "either",
    ] = "permanent_only"

    # Ranking weights
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "compensation": 0.25,
            "remote_compatibility": 0.20,
            "technical_fit": 0.20,
            "company_culture": 0.15,
            "career_growth": 0.10,
            "stability": 0.10,
        }
    )

    @field_validator(
        "experience_years",
        "minimum_base_salary",
        "preferred_base_salary",
    )
    @classmethod
    def validate_finite_values(cls, value):
        if value is not None and not isfinite(value):
            raise ValueError("Value must be finite")
        return value

    @field_validator("weights")
    @classmethod
    def validate_weight_values(cls, value):
        if any(
            not isfinite(weight) or weight < 0
            for weight in value.values()
        ):
            raise ValueError(
                "Weights must be finite and non-negative"
            )
        return value

    @model_validator(mode="after")
    def validate_profile(self):

        unknown = set(self.weights) - SUPPORTED_DIMENSIONS
        missing = SUPPORTED_DIMENSIONS - set(self.weights)

        if unknown:
            raise ValueError(
                f"Unknown dimensions: {unknown}"
            )

        if missing:
            raise ValueError(
                f"Missing dimensions: {missing}"
            )

        if not any(weight > 0 for weight in self.weights.values()):
            raise ValueError(
                "At least one dimension must have positive weight"
            )

        total = sum(self.weights.values())

        if abs(total - 1.0) >= 1e-6:
            raise ValueError(
                f"Weights must sum to 1.0; got {total}"
            )

        if (
            self.minimum_base_salary is not None
            and self.preferred_base_salary is not None
            and self.minimum_base_salary
            > self.preferred_base_salary
        ):
            raise ValueError(
                "Minimum salary cannot exceed preferred salary"
            )

        if (
            self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
        ):
            raise ValueError(
                "updated_at must be timezone-aware"
            )

        return self

CandidateProfile validation requirements
All six ranking dimensions must exist.
Zero weights are permitted.
Negative weights are rejected.
All weights must sum to 1.0 within tolerance.
At least one dimension must have positive weight.
Unknown dimensions are rejected.
Salary expectations must be finite and non-negative.
Minimum salary cannot exceed preferred salary.
Timestamps must be timezone-aware.
Unknown model fields are rejected.

Currency formatting validation must be supplemented by an ISO 4217 currency registry.

A three-letter format alone does not prove that a currency is valid.

Persisted profile revisions must be immutable. Changes produce a new revision.

6. Compensation Contract

File: src/applicant/ranking/normalization.py

Compensation normalization must be independent of job scraping and ranking calculations.

The normalization layer preserves original information and produces a canonical annual compensation representation.

python
from math import isfinite
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class NormalizedCompensation(BaseModel):

    original_minimum: float | None = None
    original_maximum: float | None = None

    original_currency: str | None = None

    original_period: Literal[
        "annual",
        "monthly",
        "weekly",
        "daily",
        "hourly",
    ] | None = None

    original_basis: Literal["gross", "net"] | None = None

    annual_minimum: float | None = None
    annual_maximum: float | None = None

    normalized_currency: str | None = None
    normalized_basis: Literal["gross", "net"] | None = None

    representative_value: float | None = None

    normalization_method: Literal[
        "minimum",
        "midpoint",
        "maximum",
    ] | None = None

    assumed_annual_hours: float | None = None

    evidence_quality: float = Field(
        default=0.0,
        ge=0,
        le=1,
    )

    warnings: list[str] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_compensation(self):

        for value in (
            self.original_minimum,
            self.original_maximum,
            self.annual_minimum,
            self.annual_maximum,
            self.representative_value,
            self.assumed_annual_hours,
        ):
            if value is not None and (
                not isfinite(value) or value < 0
            ):
                raise ValueError(
                    "Compensation values must be finite and non-negative"
                )

        if (
            self.annual_minimum is not None
            and self.annual_maximum is not None
            and self.annual_minimum > self.annual_maximum
        ):
            raise ValueError(
                "Annual minimum exceeds annual maximum"
            )

        if self.representative_value is not None:

            if (
                self.annual_minimum is not None
                and self.representative_value
                < self.annual_minimum
            ):
                raise ValueError(
                    "Representative value below minimum"
                )

            if (
                self.annual_maximum is not None
                and self.representative_value
                > self.annual_maximum
            ):
                raise ValueError(
                    "Representative value above maximum"
                )

        return self

6.1 Normalization rules
Period	Annualization
Annual	Multiply by 1
Monthly	Multiply by 12
Weekly	Multiply by 52
Daily	Multiply by configured working days
Hourly	Multiply by configured working hours

Default hourly assumption:

2,080 working hours annually.

This must be configurable and recorded in the normalization result.

6.2 Gross and net compensation

Gross and net salaries must never be directly compared as though they were equivalent.

A tax conversion would require additional jurisdiction-specific assumptions.

If the candidate expects gross compensation and the job advertises net compensation, the system must flag the comparison as incompatible or requiring review.

6.3 Salary range strategy

Eligibility and scoring must treat salary ranges differently.

Strategy	Representative value
Conservative	Minimum
Midpoint	Average of both bounds
Optimistic	Maximum

Default: midpoint.

Eligibility uses the minimum and maximum independently.

Missing salary bounds must remain null, never zero.

7. Scoring Configuration Contract

File: src/applicant/ranking/config_models.py

Scoring configurations must be independently versioned.

They capture parameters that may change independently of candidate preferences.

python
from datetime import datetime, timezone
from math import isfinite
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)


class ScoringConfiguration(BaseModel):

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
    )

    scoring_config_id: str
    config_version: str = "1.0.0"
    config_hash: str

    representative_salary_strategy: Literal[
        "minimum",
        "midpoint",
        "maximum",
    ] = "midpoint"

    evidence_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "explicit": 1.00,
            "verified": 0.90,
            "structured": 0.80,
            "inferred": 0.50,
            "fallback": 0.50,
            "unknown": 0.00,
        }
    )

    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("evidence_weights")
    @classmethod
    def validate_evidence_weights(cls, values):

        required = {
            "explicit",
            "verified",
            "structured",
            "inferred",
            "fallback",
            "unknown",
        }

        if set(values) != required:
            raise ValueError(
                "Evidence categories must match supported categories"
            )

        if any(
            not isfinite(value) or not 0 <= value <= 1
            for value in values.values()
        ):
            raise ValueError(
                "Evidence weights must be between 0 and 1"
            )

        return values

Configuration hash

config_hash must be calculated from canonical configuration data.

The hash payload excludes:

config_hash itself.
Non-deterministic creation metadata.
Other fields that do not affect scoring behaviour.

Use deterministic JSON serialization with sorted keys and SHA-256.

If an identical configuration already exists, reuse its existing immutable record.

The scoring configuration must eventually include all parameters that influence ranking, including salary strategy, evidence coefficients, scoring rules and tie-breaking policy.

8. FX Snapshot Contract

File: src/applicant/ranking/fx_models.py

FX snapshots preserve exchange rates used during ranking.

python
from datetime import datetime
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FXSnapshot(BaseModel):

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    base_currency: str
    source: str
    captured_at: datetime

    rates: dict[str, float]

    @model_validator(mode="after")
    def validate_rates(self):

        if self.captured_at.tzinfo is None:
            raise ValueError(
                "FX timestamp must be timezone-aware"
            )

        if self.base_currency not in self.rates:
            raise ValueError(
                "Base currency must have a rate"
            )

        if self.rates[self.base_currency] != 1.0:
            raise ValueError(
                "Base currency rate must equal 1.0"
            )

        if any(
            not isfinite(rate) or rate <= 0
            for rate in self.rates.values()
        ):
            raise ValueError(
                "FX rates must be finite and positive"
            )

        return self

8.1 FX rate convention

The convention must be consistent:

rates["USD"] means the number of USD units per one unit of the base currency.

For example:

Base currency: EUR.
USD rate: 1.16.
INR rate: 102.5.

These are illustrative values, not live exchange rates.

8.2 FX resolution hierarchy
Original ranking-run snapshot.
Latest available local snapshot.
Versioned static fallback rates.
Unknown currency: retain original salary and mark normalization unavailable.

Static fallback rates must never be represented as live rates.

Fallback use should:

Produce a warning.
Preserve the rate source.
Lower compensation evidence quality.
Be recorded in the ranking inputs.

FX conversion and purchasing-power parity adjustment must remain separate operations.

9. SQLite Database Schema

File: src/applicant/storage/migrations/v001_initial_schema.sql

The database uses SQLite JSON validation, foreign keys and explicit constraints.

sql
-- Migration tracking

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);


-- Versioned candidate profiles

CREATE TABLE IF NOT EXISTS candidate_profiles (
    profile_id TEXT NOT NULL,
    profile_revision INTEGER NOT NULL
        CHECK (profile_revision >= 1),

    schema_version INTEGER NOT NULL
        CHECK (schema_version >= 1),

    survey_version INTEGER NOT NULL
        CHECK (survey_version >= 1),

    updated_at TEXT NOT NULL,

    profile_data TEXT NOT NULL
        CHECK (json_valid(profile_data)),

    is_active INTEGER NOT NULL DEFAULT 1
        CHECK (is_active IN (0, 1)),

    PRIMARY KEY (profile_id, profile_revision)
);


CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_profile
ON candidate_profiles(profile_id)
WHERE is_active = 1;


-- FX snapshots

CREATE TABLE IF NOT EXISTS fx_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    base_currency TEXT NOT NULL,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,

    rates_json TEXT NOT NULL
        CHECK (json_valid(rates_json))
);


-- Scoring configurations

CREATE TABLE IF NOT EXISTS scoring_configs (
    scoring_config_id TEXT PRIMARY KEY,

    config_version TEXT NOT NULL,
    config_hash TEXT NOT NULL UNIQUE,

    config_data TEXT NOT NULL
        CHECK (json_valid(config_data)),

    created_at TEXT NOT NULL
);


-- Immutable job versions

CREATE TABLE IF NOT EXISTS job_versions (
    job_version_id TEXT PRIMARY KEY,

    job_id TEXT NOT NULL,

    content_hash TEXT NOT NULL,
    ranking_hash TEXT NOT NULL,

    job_data TEXT NOT NULL
        CHECK (json_valid(job_data)),

    captured_at TEXT NOT NULL
);


CREATE INDEX IF NOT EXISTS idx_job_versions_lookup
ON job_versions(job_id, ranking_hash);


CREATE INDEX IF NOT EXISTS idx_job_versions_job
ON job_versions(job_id, captured_at DESC);


-- Ranking runs

CREATE TABLE IF NOT EXISTS ranking_runs (
    ranking_run_id TEXT PRIMARY KEY,

    profile_id TEXT NOT NULL,
    profile_revision INTEGER NOT NULL,

    algorithm_version TEXT NOT NULL,

    fx_snapshot_id TEXT,
    scoring_config_id TEXT NOT NULL,

    calculated_at TEXT NOT NULL,

    FOREIGN KEY (profile_id, profile_revision)
        REFERENCES candidate_profiles(
            profile_id,
            profile_revision
        ),

    FOREIGN KEY (fx_snapshot_id)
        REFERENCES fx_snapshots(snapshot_id),

    FOREIGN KEY (scoring_config_id)
        REFERENCES scoring_configs(scoring_config_id)
);


CREATE INDEX IF NOT EXISTS idx_ranking_runs_profile
ON ranking_runs(
    profile_id,
    calculated_at DESC,
    ranking_run_id DESC
);


-- Ranking outputs

CREATE TABLE IF NOT EXISTS job_rankings (
    ranking_run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    job_version_id TEXT NOT NULL,

    overall_score REAL NOT NULL
        CHECK (overall_score >= 0 AND overall_score <= 100),

    confidence REAL NOT NULL
        CHECK (confidence >= 0 AND confidence <= 1),

    dimension_scores TEXT NOT NULL
        CHECK (json_valid(dimension_scores)),

    ranking_data TEXT NOT NULL
        CHECK (json_valid(ranking_data)),

    PRIMARY KEY (ranking_run_id, job_id),

    FOREIGN KEY (ranking_run_id)
        REFERENCES ranking_runs(ranking_run_id),

    FOREIGN KEY (job_version_id)
        REFERENCES job_versions(job_version_id)
);


CREATE INDEX IF NOT EXISTS idx_job_rankings_score
ON job_rankings(ranking_run_id, overall_score DESC);


CREATE INDEX IF NOT EXISTS idx_job_rankings_job
ON job_rankings(job_id);

9.1 Database integrity requirements
Enable foreign keys on every database connection.
Enforce one active profile revision per profile.
Preserve historical profile revisions.
Preserve immutable job versions.
Preserve completed ranking runs.
Validate JSON payloads.
Enforce score and confidence bounds.
Prevent deletion of records referenced by historical ranking runs.
Use transactions for related writes.

Historical profile records are immutable except for the controlled is_active transition required during revision creation.

10. Database Infrastructure
10.1 Connection configuration

File: src/applicant/storage/connection.py

Every SQLite connection must configure:

sql
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;


Enable WAL during database initialization:

sql
PRAGMA journal_mode = WAL;


Verify that SQLite actually returns WAL as the active journal mode.

WAL improves concurrent reading and writing behaviour but does not eliminate write contention.

Keep transactions short.

10.2 Migration management

File: src/applicant/storage/database.py

Responsibilities:

Resolve database location.
Create the database if required.
Initialize connection settings.
Read migration history.
Apply pending migrations in order.
Record successfully applied migration versions.
Roll back failed transactional migrations.

Do not use CREATE TABLE IF NOT EXISTS as a substitute for schema migrations.

A future schema upgrade must be represented by a new migration.

10.3 Migration versioning

Initial migration:

v001_initial_schema.sql

Future migrations should follow sequential naming:

text
v002_profile_constraints.sql
v003_ranking_metadata.sql
v004_job_version_metadata.sql


Each migration should be independently testable.

11. Persistence and Repository Layer

Repositories encapsulate database operations.

11.1 ProfileRepository

File: src/applicant/storage/repositories/profiles.py

Responsibilities:

Save candidate profiles.
Load active profiles.
Load historical profile revisions.
Create new revisions.
Validate revision consistency.
Prevent accidental historical modification.
Transactional revision creation

The profile revision process must be atomic.

text
BEGIN EXCLUSIVE
       |
       v
Read MAX(profile_revision)
       |
       v
Calculate next revision
       |
       v
Check expected revision
       |
       v
Deactivate previous revision
       |
       v
Insert new revision
       |
       v
COMMIT


If any operation fails, roll back everything.

Use a separate SQLite connection for each concurrent operation.

Configure a busy timeout.

Implement optimistic concurrency checking: if a process started editing revision 3 but revision 4 has already been saved, reject the stale update rather than silently overwriting preferences.

Do not perform interactive prompts or network operations inside the transaction.

11.2 FXSnapshotRepository

File: src/applicant/storage/repositories/fx_snapshots.py

Responsibilities:

Persist FX snapshots.
Retrieve snapshots by ID.
Retrieve the latest eligible local snapshot.
Validate currencies and rates.
Preserve source and capture timestamps.

Snapshots must be immutable after insertion.

11.3 ScoringConfigRepository

File: src/applicant/storage/repositories/scoring_configs.py

Responsibilities:

Validate scoring configurations.
Calculate canonical hashes.
Deduplicate identical configurations.
Persist immutable configuration records.
Retrieve configurations by ID or hash.
11.4 JobVersionRepository

File: src/applicant/storage/repositories/job_versions.py

Responsibilities:

Generate canonical job fingerprints.
Preserve original and normalized job data.
Detect material changes.
Retrieve exact job versions.
Prevent modification or deletion of historical versions.

Use SHA-256.

Maintain two fingerprints:

content_hash: relevant normalized job content.
ranking_hash: fields that affect eligibility and scoring.

Cosmetic changes should not unnecessarily invalidate rankings.

Relevant job changes include:

Salary.
Location.
Remote eligibility.
Employment type.
Job description.
Experience requirements.
Technical requirements.

The job version should also preserve relevant extraction metadata, including parser version and normalized compensation.

11.5 RankingRunRepository

File: src/applicant/storage/repositories/ranking_runs.py

Responsibilities:

Create ranking run records.
Associate exact candidate profile revisions.
Associate exact FX snapshots.
Associate exact scoring configurations.
Preserve algorithm versions.
Associate job rankings with immutable job versions.
Retrieve historical runs and their complete inputs.

Completed ranking runs and their ranking outputs must not be silently overwritten.

A refresh creates a new run.

12. Fingerprinting and Historical Integrity

Job fingerprints are calculated from deterministic canonical JSON.

Example:

python
import hashlib
import json


def calculate_job_fingerprint(job: dict) -> str:

    relevant_fields = {
        "title": job.get("title"),
        "description": job.get("description"),
        "salary": job.get("salary"),
        "location": job.get("location"),
        "work_mode": job.get("work_mode"),
        "employment_type": job.get("employment_type"),
        "company": job.get("company"),
    }

    canonical = json.dumps(
        relevant_fields,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


The actual implementation should use the existing Applicant job schema rather than assuming these field names.

Canonicalization must normalize irrelevant formatting differences before hashing.

A ranking hash should only include attributes that affect ranking.

12.1 Ranking freshness

Future ranking functionality should identify:

Current.
Job changed.
Profile changed.
Algorithm changed.
FX refresh available.
Scoring configuration changed.
Unknown.

A newer FX snapshot does not invalidate an old ranking. It only makes a new ranking possible.

Historical ranking inputs must always remain accessible.

13. Milestone 1 Scope

Milestone 1 implements:

Candidate profile models.
Scoring configuration models.
Normalized compensation contract.
FX snapshot contract.
SQLite initialization.
Initial database migration.
Migration tracking.
Profile persistence.
Transactional profile revision management.
FX snapshot persistence.
Scoring configuration persistence.
Job version persistence.
Ranking run persistence.
Unit and integration tests.
13.1 Explicitly excluded from Milestone 1

The following are future work:

Interactive survey implementation.
Adaptive questionnaire logic.
JSON-backed survey CLI.
Salary normalization algorithms.
Live FX retrieval.
Eligibility engine.
Weighted ranking calculations.
Confidence calculation.
Ranking explanations.
applicant rank CLI.
Automatic preference learning.
Recommendation interfaces.

The contracts should support these future capabilities without implementing their business logic in Milestone 1.

14. Implementation Sequence

The following order minimizes dependencies and rework.

Order	Implementation task	Priority
1	Inspect existing Applicant architecture	P0
2	Implement CandidateProfile	P0
3	Implement ScoringConfiguration	P0
4	Implement NormalizedCompensation	P0
5	Implement FXSnapshot	P0
6	Implement initial SQLite migration	P0
7	Implement database initialization	P0
8	Implement ProfileRepository	P0
9	Implement FXSnapshotRepository	P0
10	Implement ScoringConfigRepository	P0
11	Implement JobVersionRepository	P0
12	Implement RankingRunRepository	P0
13	Implement integration tests	P0
14.1 Repository reconnaissance

Before changing code, inspect:

Existing source package structure.
Existing database implementation.
Existing job data model.
Existing SQLite schema.
Current CLI framework and entry points.
Pydantic version.
Python version requirements.
Existing test conventions.
Existing dependency management.
Current database file location.

Reuse existing abstractions where appropriate.

Avoid creating duplicate database connection managers, migration systems or job models.

15. Testing Strategy

Testing is mandatory before Milestone 1 is considered complete.

15.1 Pydantic tests

Candidate profile tests must verify:

Valid profile creation.
Default weight assignment.
Zero-weight dimensions.
Negative weight rejection.
All-zero weight rejection.
Weight sum validation.
Unknown dimension rejection.
Missing dimension rejection.
Non-finite salary rejection.
Invalid salary expectation rejection.
Invalid currency format rejection.
Extra field rejection.
Timezone-aware timestamps.

Scoring configuration tests must verify:

Valid configuration creation.
Evidence category validation.
Evidence coefficient boundaries.
Non-finite coefficient rejection.
Canonical hash consistency.
Identical configuration deduplication.

Compensation contract tests must verify:

Valid salary ranges.
Invalid salary ranges.
Representative values within bounds.
Evidence quality boundaries.
Missing salary values.
Non-finite monetary values.

FX snapshot tests must verify:

Base currency presence.
Base currency rate equals 1.0.
Positive finite rates.
Invalid timestamps.
Unsupported currencies.
Correct rate convention.
15.2 Database migration tests

Verify:

Fresh database creation.
Migration tracking.
Repeated initialization.
Foreign key enforcement.
JSON constraints.
Partial unique active-profile index.
Score and confidence constraints.
Migration rollback behaviour.
WAL initialization.
Existing database compatibility.
15.3 Profile repository tests

Verify:

Initial profile creation.
Profile revision increment.
Historical revision preservation.
Exactly one active revision.
Invalid update rollback.
Concurrent update handling.
Stale revision rejection.
Profile serialization and reload.
15.4 Historical persistence tests

Verify:

FX snapshot persistence.
Scoring configuration persistence.
Job version persistence.
Job fingerprint stability.
Job version immutability.
Ranking run persistence.
Ranking output persistence.
Recovery of all five original ranking inputs.

Use temporary SQLite databases for isolated tests.

16. Milestone 1 Acceptance Criteria

Milestone 1 is complete only when all the following conditions are satisfied.

Candidate profile integrity
Candidate profiles validate successfully.
All six ranking dimensions are represented.
Zero-weight dimensions are supported.
Invalid weights are rejected.
Salary invariants are enforced.
Schema and survey versions are stored.
Profile revisions are preserved.
Concurrent updates cannot create duplicate revisions.
Historical profile data cannot be silently modified.
Compensation and FX contracts
Original salary information is preserved.
Annualized salary fields are defined.
Gross and net compensation are distinguished.
Missing salary bounds remain null.
FX rate direction is documented.
FX snapshots are validated and immutable.
Fallback FX provenance can be represented.
Scoring configuration
Scoring configuration has an independent identity.
Configuration versions are stored.
Canonical configuration hashes are deterministic.
Identical configurations can be deduplicated.
Scoring configuration records are immutable.
Job version integrity
Job content fingerprints are deterministic.
Ranking fingerprints identify material changes.
Exact job versions are retrievable.
Historical job versions are immutable.
Relevant parser and normalization metadata are retained.
Ranking persistence
Ranking runs reference profile revisions.
Ranking runs reference scoring configurations.
Ranking runs reference FX snapshots where applicable.
Ranking outputs reference exact job versions.
Ranking results preserve algorithm versions.
Historical ranking runs can be retrieved.
All five original inputs can be recovered.
Operational integrity
Foreign keys are enabled on every connection.
Database migrations are tracked.
WAL initialization is verified.
Transactions roll back on failure.
Database constraints are tested.
All relevant unit and integration tests pass.
17. Future Milestones

After Milestone 1 is complete, development proceeds in the following sequence.

Milestone 2 — Eligibility and Ranking Engine

Implement:

Salary annualization and currency normalization.
FX resolution and fallback behaviour.
Remote eligibility classification.
Employment eligibility.
Compensation floor evaluation.
Three-state eligibility.
Weighted opportunity scoring.
Evidence quality assessment.
Confidence calculations.
Deterministic tie-breaking.

Eligibility states:

python
ELIGIBLE = "eligible"
REVIEW_REQUIRED = "review_required"
EXCLUDED = "excluded"


Unknown information must not automatically exclude an opportunity.

Zero-weight dimensions must be excluded from both the overall score and confidence denominator.

Milestone 3 — Survey and Ranking CLI

See [the repository-reviewed M3 plan](docs/milestone-3-plan.md) for the command
contract, profile concurrency, input/provenance formats, read-only history,
conflict reporting, scope amendments, semantic commits and coverage gates.
The commands below are the original proposal, not the finalized interface.

Proposed commands:

bash
uv run applicant survey
uv run applicant survey --show
uv run applicant survey --export profile.json
uv run applicant survey --from-file profile.json
uv run applicant survey --validate profile.json
uv run applicant survey --dry-run --from-file profile.json

uv run applicant rank
uv run applicant rank --top 20
uv run applicant rank --region Europe
uv run applicant rank --explain JOB_ID
uv run applicant rank --refresh-stale
uv run applicant rank --force


The survey must support interactive and non-interactive operation.

Configuration imports must be validated before database modification.

Milestone 4 — Personalization and Evaluation

Potential future capabilities:

Shortlisting feedback.
Rejection reason tracking.
Candidate preference adjustments.
Historical ranking comparisons.
Recommendation quality evaluation.
Optional adaptive weight suggestions.
Compensation benchmarking.
Improved company culture and stability evidence.

Adaptive preference changes must always be explicit and auditable.

18. Final Architectural Decisions

The following decisions are considered fixed for the initial implementation.

Decision	Resolution
Profile versioning	Separate schema, survey and revision versions
Profile persistence	Immutable historical revisions
Profile concurrency	Transactional revision creation
Compensation representation	Preserve original and normalized values
Compensation period	Canonical annual representation
Gross/net comparison	Never silently mix
FX conversion	Snapshot-based
Offline FX	Cached data and disclosed static fallback
Currency conversion	Separate from PPP
Scoring weights	Non-negative, sum to 1.0, zero permitted
Confidence	Evidence quality, independent of fit
Job versioning	SHA-256 fingerprints
Job freshness	Material-change detection
Ranking persistence	Immutable ranking runs
Scoring configuration	Independently versioned and hashed
Database	SQLite with migrations and foreign keys
Concurrency	Short transactional writes
Testing	Unit, migration and integration tests
19. Final Implementation Directive

The immediate engineering objective is to implement Milestone 1 without modifying existing Applicant discovery and application workflows unnecessarily.

The implementation should begin with repository reconnaissance, followed by Pydantic data contracts and the initial SQLite migration.

Next, implement the repositories and verify the complete persistence workflow.

Do not implement survey interaction, eligibility decisions, scoring calculations or recommendation logic during this milestone.

The deliverable is a tested, stable foundation that can preserve candidate preferences, compensation metadata, FX snapshots, scoring configurations, job versions and ranking run references.

Definition of success:

Applicant must be able to preserve and recover every input needed for a future ranking operation, independently of subsequent changes to the underlying data or application logic.

This establishes the foundation for a transparent, deterministic, explainable and auditable job recommendation system.
