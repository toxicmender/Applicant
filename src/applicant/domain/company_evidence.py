"""Immutable, explicitly verified company associations and capture envelopes."""

import json
import re
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .benchmarks import BenchmarkResponse
from .canonical import canonical_json, content_hash
from .contracts import Contract, Identity, Timestamp

CompanyProvider = Literal['glassdoor', 'ambitionbox', 'crunchbase', 'tracxn']


def _decimal_numbers(value):
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimal_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimal_numbers(v) for v in value]
    return value


class CompanyAssociation(Contract):
    kind: Literal['company_association'] = 'company_association'
    association_id: Identity
    job_id: Identity
    job_version_id: Identity
    provider: CompanyProvider
    provider_company_id: Identity
    locator: Identity
    verified_at: Timestamp
    verified_by: Identity
    verification_note: Identity
    verification_url: Identity

    @model_validator(mode='after')
    def exact_locator(self):
        if self.provider == 'ambitionbox':
            valid = re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', self.locator)
        elif self.provider == 'glassdoor':
            valid = re.fullmatch(
                r'https://www\.glassdoor\.com/Reviews/[^/?#]+-Reviews-E([0-9]+)\.htm', self.locator
            )
            valid = valid and valid[1] == self.provider_company_id
        elif self.provider == 'crunchbase':
            valid = (
                self.locator
                == 'https://www.crunchbase.com/organization/' + self.provider_company_id
                and re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', self.provider_company_id)
            )
        else:
            valid = (
                re.fullmatch(r'[0-9a-f]{24}', self.provider_company_id)
                and (
                    self.locator == self.provider_company_id
                    or re.fullmatch(
                        r'https://tracxn\.com/d/companies/[^/?#]+/__[^/?#]+', self.locator
                    )
                )
            ) or (
                self.provider_company_id.startswith('__')
                and re.fullmatch(
                    r'https://tracxn\.com/d/companies/[^/?#]+/'
                    + re.escape(self.provider_company_id),
                    self.locator,
                )
            )
        if not valid:
            raise ValueError(
                'association requires an exact provider locator matching its company ID (AmbitionBox uses a verified slug)'
            )
        return self


class CompanyResponse(BenchmarkResponse):
    representation: Literal['http_response', 'browser_response', 'browser_dom'] = 'http_response'


class CompanyEvidenceSnapshot(Contract):
    kind: Literal['company_evidence_snapshot'] = 'company_evidence_snapshot'
    snapshot_id: Identity
    association_id: Identity
    provider: CompanyProvider
    provider_company_id: Identity
    acquired_at: Timestamp
    observed_at: Timestamp | None = None
    observation_note: Identity
    adapter_version: Literal['company-capture-1'] = 'company-capture-1'
    payload: dict
    responses: tuple[CompanyResponse, ...] = Field(min_length=1)

    @field_validator('payload', mode='before')
    @classmethod
    def normalized_numbers(cls, value):
        # Legacy provider floats and strict JSON Decimals share one replay format.
        # Original source bytes remain in responses; this does not add precision.
        return json.loads(canonical_json(_decimal_numbers(value)))

    @model_validator(mode='after')
    def integrity(self):
        if self.observed_at is not None and self.observed_at > self.acquired_at:
            raise ValueError('company observation cannot follow acquisition')
        if (
            self.payload.get('source') != self.provider
            or self.payload.get('company_id') != self.provider_company_id
        ):
            raise ValueError('returned provider/company ID differs from the association')
        if len({r.response_id for r in self.responses}) != len(self.responses):
            raise ValueError('duplicate company raw response')
        if (
            len(self.responses) > 64
            or any(len(r.body) > 8_000_000 for r in self.responses)
            or sum(len(r.body) for r in self.responses) > 16_000_000
        ):
            raise ValueError('company raw capture exceeds byte/response limits')
        content_hash(self.payload)
        return self

    @property
    def snapshot_hash(self):
        return content_hash(self.model_dump(exclude={'snapshot_id'}))


class CompanyEvidenceImport(Contract):
    kind: Literal['company_evidence_import'] = 'company_evidence_import'
    associations: tuple[CompanyAssociation, ...] = ()
    snapshots: tuple[CompanyEvidenceSnapshot, ...] = ()

    @model_validator(mode='after')
    def unique(self):
        if len({a.association_id for a in self.associations}) != len(self.associations) or len(
            {s.snapshot_id for s in self.snapshots}
        ) != len(self.snapshots):
            raise ValueError('duplicate association or snapshot IDs')
        return self


class CompanySelection(Contract):
    association_id: Identity
    snapshot_id: Identity


class CompanyEnrichment(Contract):
    kind: Literal['company_enrichment'] = 'company_enrichment'
    selections: tuple[CompanySelection, ...] = Field(min_length=1)

    @model_validator(mode='after')
    def unique(self):
        if len({(s.association_id, s.snapshot_id) for s in self.selections}) != len(
            self.selections
        ):
            raise ValueError('duplicate company evidence selection')
        return self
