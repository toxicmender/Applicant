"""Validate provider import provenance before opening a store."""

from datetime import date
from typing import Literal

from pydantic import Field, ValidationError

from ..domain.canonical import content_hash
from ..domain.providers import provider
from ..domain.research import Compensation, parse_import
from ..domain.source_operations import Contract, Destination, OperationName
from ..errors import ConfigError

_RESTRICTED = {'levels', 'blind', 'upwork', 'arc', 'welcometothejungle'}


class ImportManifest(Contract):
    provider: str
    operation: OperationName
    dataset: str = Field(min_length=1)
    acquired_on: date
    rights_basis: Literal['own_observation', 'licensed_export', 'written_permission']
    agreement_reference: str | None = Field(default=None, min_length=1)
    destinations: tuple[Destination, ...]
    source_batch_id: str | None = Field(default=None, min_length=1)


class SourceRecord(Contract):
    source_record_id: str | None = Field(default=None, min_length=1)
    row_locator: str | None = Field(default=None, min_length=1)
    metric: str | None = Field(default=None, min_length=1)


class ProviderImport(Contract):
    manifest: ImportManifest
    bundle: dict
    records: dict[str, SourceRecord] = Field(default_factory=dict)


def compensation_revisions(bundle, manifest, records, artifact_digest):
    """Assign immutable revisions using source locations, independent of pay values."""
    batch = manifest.source_batch_id or artifact_digest
    evidence = []
    for index, item in enumerate(bundle.evidence, start=1):
        if not isinstance(item.payload, Compensation):
            raise ConfigError('compensation import requires compensation observations')
        record = records.get(item.evidence_id, SourceRecord())
        if record.source_record_id:
            identity = ('source_record', record.source_record_id)
        else:
            identity = ('batch_row', batch, record.row_locator or str(index))
        metric = record.metric or item.payload.component
        logical = content_hash((provider(manifest.provider).id, manifest.dataset, identity, metric))
        payload = item.model_dump(mode='json', exclude={'evidence_id', 'acquired_at'})
        revision = content_hash(payload)
        evidence.append(item.model_copy(update={'evidence_id': f'comp-{logical}-{revision}'}))
    return bundle.model_copy(update={'evidence': tuple(evidence)})


def validate_source_import(
    value, *, expected=None, destination='persist', fixture=False, artifact_digest=None
):
    """Return a validated generic bundle, without retaining agreement details."""
    envelope = isinstance(value, dict) and 'manifest' in value
    if envelope:
        try:
            document = ProviderImport.model_validate(value)
        except ValidationError:
            raise ConfigError('invalid provider import manifest or bundle') from None
        manifest = document.manifest
        try:
            name = provider(manifest.provider).id
        except ValueError:
            raise ConfigError('unknown provider in import manifest') from None
        operation = manifest.operation
        if expected and (name, operation) != expected:
            raise ConfigError('import manifest does not match the selected provider and operation')
        from .source_operations import operations

        configured = operations(provider(name)).get(operation)
        if configured is None or not any(
            route.method == 'user_import' and route.policy == 'allowed'
            for route in configured.routes
        ):
            raise ConfigError('provider operation has no authorized import route')
        if destination not in manifest.destinations:
            raise ConfigError('import manifest does not authorize the requested destination')
        if (
            not fixture
            and name in _RESTRICTED
            and (manifest.rights_basis != 'written_permission' or not manifest.agreement_reference)
        ):
            raise ConfigError('this provider import requires a written permission reference')
        value = document.bundle
    elif expected:
        raise ConfigError('provider import requires a provenance manifest')

    bundle = parse_import(value)
    sources = {
        *(item.provider for item in bundle.evidence),
        *(item.provider for item in bundle.identifiers),
        *(item.job.source for item in bundle.observations),
    }
    aliases = {'levels.fyi': 'levels', 'otta': 'welcometothejungle', 'wwr': 'weworkremotely'}
    names = {aliases.get(source, source) for source in sources}
    if envelope and names - {name}:
        raise ConfigError('provider import contains records from a different source')
    if envelope and operation == 'compensation':
        if not bundle.evidence or any(
            item.payload.kind != 'compensation' for item in bundle.evidence
        ):
            raise ConfigError('compensation import requires compensation observations')
        return compensation_revisions(
            bundle,
            manifest,
            document.records,
            artifact_digest or content_hash(value),
        )
    if not envelope and names & _RESTRICTED:
        raise ConfigError('restricted source requires a provider import manifest')
    return bundle


def sample_import(name, operation):
    """Supply a minimal synthetic provider import example for the offline probe."""
    from .research import template

    kind = {
        'compensation': 'compensation',
        'workplace_policy': 'policy',
        'workplace_reviews': 'culture',
        'company_profile': 'culture',
    }.get(operation, 'job-party')
    bundle = template(kind)
    for observation in bundle['observations']:
        observation['job']['source'] = name
    for item in bundle['evidence']:
        item['provider'] = name
    return {
        'manifest': {
            'provider': name,
            'operation': operation,
            'dataset': 'synthetic',
            'acquired_on': '2026-01-01',
            'rights_basis': 'own_observation',
            'destinations': ['display'],
        },
        'bundle': bundle,
    }
