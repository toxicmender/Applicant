"""Versioned research contracts, deliberately independent of ranking inputs."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .canonical import content_hash
from .company_evidence import CompanyResponse
from .contracts import CurrencyCode, Identity, Money, Timestamp
from .job import Job


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, validate_default=True)
    schema_version: Literal[1] = 1


class Company(Record):
    kind: Literal['company'] = 'company'
    entity_id: Identity
    name: Identity
    recorded_at: Timestamp
    source_url: Identity


class CompanyName(Record):
    kind: Literal['company_name'] = 'company_name'
    name_id: Identity
    entity_id: Identity
    name: Identity
    name_type: Literal['legal', 'trading', 'historical', 'alias'] = 'alias'
    effective_from: date | None = None
    effective_to: date | None = None
    recorded_at: Timestamp
    source_url: Identity

    @model_validator(mode='after')
    def dates(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError('name validity ends before it starts')
        return self


class CompanyIdentifier(Record):
    kind: Literal['company_identifier'] = 'company_identifier'
    identifier_id: Identity
    entity_id: Identity
    provider: Identity
    provider_company_id: Identity
    verified_at: Timestamp
    verification_url: Identity
    effective_from: date | None = None
    effective_to: date | None = None

    @model_validator(mode='after')
    def validity(self):
        if self.effective_from and self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError('identifier validity must end after it starts')
        return self


class CompanyEvent(Record):
    kind: Literal['company_event'] = 'company_event'
    event_id: Identity
    event_type: Literal['rename', 'acquisition', 'merger', 'spin_off', 'subsidiary']
    subject_id: Identity
    related_id: Identity | None = None
    effective_date: date | None = None
    recorded_at: Timestamp
    source_url: Identity
    note: Identity
    supersedes: Identity | None = None

    @model_validator(mode='after')
    def relation(self):
        if self.supersedes == self.event_id:
            raise ValueError('an event cannot supersede itself')
        if self.event_type != 'rename' and (
            not self.related_id or self.related_id == self.subject_id
        ):
            raise ValueError('corporate relationship requires distinct entities')
        if self.event_type == 'rename' and self.related_id is not None:
            raise ValueError('rename retains the subject entity')
        return self


class ListingObservation(Record):
    kind: Literal['listing_observation'] = 'listing_observation'
    job_version_id: Identity
    job: Job
    acquired_at: Timestamp
    observed_at: Timestamp | None = None
    source_url: Identity
    content_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    content_representation: Literal['normalized_job', 'http_response'] = 'normalized_job'
    employer_entity_id: Identity | None = None
    employer_requisition_id: Identity | None = None
    employer_requisition_url: str | None = None
    agency_requisition_id: Identity | None = None
    origin: Literal['direct', 'agency', 'unknown'] = 'unknown'
    status: Literal['open', 'closed', 'unknown'] = 'unknown'
    syndication_origin: Identity | None = None

    @field_validator('employer_requisition_url')
    @classmethod
    def url(cls, value):
        if value is not None:
            url = urlsplit(value)
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise ValueError('employer requisition URL must be HTTPS without credentials')
        return value

    @model_validator(mode='after')
    def chronology(self):
        if self.observed_at and self.observed_at > self.acquired_at:
            raise ValueError('source observation cannot follow acquisition')
        return self


class JobParty(Record):
    kind: Literal['job_party'] = 'job_party'
    party_id: Identity
    job_version_id: Identity
    role: Literal['advertiser', 'agency', 'hiring_employer', 'employer_of_record']
    entity_id: Identity | None = None
    source_url: Identity
    excerpt: Identity
    recorded_at: Timestamp | None = None


class Compensation(Record):
    kind: Literal['compensation'] = 'compensation'
    minimum: Money | None = None
    maximum: Money | None = None
    currency: CurrencyCode
    period: Literal['annual', 'monthly', 'weekly', 'daily', 'hourly', 'unknown'] = 'unknown'
    basis: Literal['gross', 'net', 'unknown'] = 'unknown'
    component: Literal['base', 'bonus', 'equity', 'total', 'unknown'] = 'unknown'
    measurement: Literal['posting', 'submission', 'estimate', 'benchmark'] = 'posting'
    equity_percent: Decimal | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    role: str | None = None
    specialization: str | None = None
    level: str | None = None
    geography: str | None = None
    sample_size: int | None = Field(default=None, ge=1)
    methodology: str | None = None

    @model_validator(mode='after')
    def bounds(self):
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('compensation minimum exceeds maximum')
        return self


class Culture(Record):
    kind: Literal['culture'] = 'culture'
    topic: Literal[
        'work_life_balance',
        'on_call',
        'technology',
        'growth',
        'technical_debt',
        'culture',
        'company',
    ]
    description: Identity
    rating: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    scale: str | None = None


class Schedule(Record):
    employer_timezone: str
    start: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    fold: Literal[0, 1] | None = None


class Workplace(Record):
    kind: Literal['policy'] = 'policy'
    modes: tuple[Literal['Remote', 'Hybrid', 'Onsite'], ...] = ()
    allowed_countries: tuple[str, ...] = ()
    remote_scope: Literal['worldwide', 'country_list', 'unknown'] = 'unknown'
    office_days: int | None = Field(default=None, ge=0, le=7)
    description: Identity
    effective_date: date | None = None
    enforcement: bool = False
    schedule: Schedule | None = None


class Technology(Record):
    kind: Literal['technology'] = 'technology'
    technology: Identity
    requirement: Literal['required', 'preferred', 'mentioned']
    alternative_group: Identity | None = None
    negated: bool = False


Payload = Annotated[Compensation | Culture | Workplace | Technology, Field(discriminator='kind')]


class ResearchEvidence(Record):
    kind: Literal['research_evidence'] = 'research_evidence'
    evidence_id: Identity
    entity_id: Identity | None = None
    job_version_id: Identity | None = None
    provider: Identity
    source_url: Identity
    acquired_at: Timestamp
    observed_at: Timestamp | None = None
    scope: Literal['job', 'team', 'location', 'company', 'cohort']
    assertion: Literal['employer', 'employee', 'platform', 'editorial', 'import'] = 'import'
    excerpt: Identity
    extractor_version: Identity = 'research-import-1'
    payload: Payload
    responses: tuple[CompanyResponse, ...] = ()

    @model_validator(mode='after')
    def provenance(self):
        if (
            len(self.responses) > 64
            or sum(len(response.body) for response in self.responses) > 16_000_000
        ):
            raise ValueError('research raw capture exceeds limits')
        if self.observed_at and self.observed_at > self.acquired_at:
            raise ValueError('observation cannot follow acquisition')
        if self.scope == 'job' and not self.job_version_id:
            raise ValueError('job evidence requires an exact job version')
        if self.payload.kind == 'technology' and not self.job_version_id:
            raise ValueError('technology facts require an exact job version')
        return self


class ResearchImport(Record):
    kind: Literal['research_import'] = 'research_import'
    companies: tuple[Company, ...] = ()
    names: tuple[CompanyName, ...] = ()
    identifiers: tuple[CompanyIdentifier, ...] = ()
    events: tuple[CompanyEvent, ...] = ()
    observations: tuple[ListingObservation, ...] = ()
    parties: tuple[JobParty, ...] = ()
    evidence: tuple[ResearchEvidence, ...] = ()


def record_hash(value):
    return content_hash(value)
