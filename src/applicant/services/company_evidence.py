"""Explicit capture/import and offline enrichment; ranking never refreshes sources."""

from collections.abc import Callable
from typing import Any

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from ..domain.canonical import content_hash, utc
from ..domain.company_evidence import (
    CompanyEnrichment,
    CompanyEvidenceImport,
    CompanyEvidenceSnapshot,
)
from ..domain.contracts import now
from ..domain.ranking import Evidence, JobVersion
from ..domain.ranking_inputs import JobVersionBundle
from ..errors import SourceError, StoreError
from ..financials.models import CompanyFinancials, FundingRound, Money
from ..infra.company_sources import CompanyRecorder, company_client
from ..infra.store.company_evidence import CompanyEvidenceRepository
from ..infra.store.job_versions import JobVersionRepository
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.sqlite import Store
from ..reviews.models import CompanyRating, Review
from ..settings import Settings
from .ranking_evidence import capture_rating


def _object(value, keys):
    if not isinstance(value, dict) or not value.keys() <= keys:
        raise ValueError('company payload contains an unknown field or invalid object')


def rating_payload(value) -> CompanyRating:
    _object(value, set(CompanyRating.__dataclass_fields__))
    reviews = value.get('reviews', [])
    if not isinstance(reviews, list):
        raise ValueError('company reviews must be an array')
    for review in reviews:
        _object(review, set(Review.__dataclass_fields__) - {'source'})
    try:
        breakdown = value.get('rating_breakdown', {})
        if not isinstance(breakdown, dict):
            raise ValueError('company category ratings must be an object')
        breakdown = {k: Decimal(v) if isinstance(v, str) else v for k, v in breakdown.items()}
        rating = CompanyRating(
            **(value | {'rating_breakdown': breakdown, 'reviews': [Review(**r) for r in reviews]})
        )
        # Validate category/count policies even when observation time is unknown.
        capture_rating(rating, verified_company_id=rating.company_id or '', observed_at=now())
    except (TypeError, AttributeError, InvalidOperation) as error:
        raise ValueError(f'invalid company rating: {error}') from error
    return rating


def validate_payload(snapshot: CompanyEvidenceSnapshot):
    if snapshot.provider in ('glassdoor', 'ambitionbox'):
        rating_payload(snapshot.payload)
    else:
        data = snapshot.payload
        _object(data, CompanyFinancials.model_fields.keys())
        for field in ('total_funding', 'valuation', 'revenue'):
            if data.get(field) is not None:
                _object(data[field], Money.model_fields.keys())
        rounds = data.get('rounds', [])
        if not isinstance(rounds, list):
            raise ValueError('company funding rounds must be an array')
        for row in rounds:
            _object(row, FundingRound.model_fields.keys())
            for field in ('amount', 'post_money_valuation'):
                if row.get(field) is not None:
                    _object(row[field], Money.model_fields.keys())
        CompanyFinancials.model_validate(data)


def import_company_evidence(
    database: Path, document: CompanyEvidenceImport, *, dry_run: bool = False
) -> dict[str, Any]:
    document = validated(document)
    for snapshot in document.snapshots:
        validate_payload(snapshot)
    # A dry run needs existing job versions, but never creates/upgrades a database.
    with Store.open_readonly(database) if dry_run else Store(database) as store:
        repo = CompanyEvidenceRepository(store.db)
        if not dry_run:
            with write_transaction(store.db):
                for association in document.associations:
                    repo.put_association(association)
                for snapshot in document.snapshots:
                    repo.put_snapshot(snapshot)
        else:
            with store.read_snapshot():
                supplied = {a.association_id: a for a in document.associations}
                for association in document.associations:
                    repo.validate_association(association)
                    if (
                        store.db.execute(
                            'SELECT 1 FROM company_associations WHERE association_id=?',
                            (association.association_id,),
                        ).fetchone()
                        and repo.association(association.association_id) != association
                    ):
                        raise StoreError('association ID already contains different inputs')
                for snapshot in document.snapshots:
                    association = supplied.get(snapshot.association_id) or repo.association(
                        snapshot.association_id
                    )
                    _matches(association, snapshot)
                    if (
                        store.db.execute(
                            'SELECT 1 FROM company_evidence_snapshots WHERE snapshot_id=?',
                            (snapshot.snapshot_id,),
                        ).fetchone()
                        and repo.snapshot(snapshot.snapshot_id) != snapshot
                    ):
                        raise StoreError('company snapshot ID already contains different inputs')
    return {
        'kind': 'company_evidence_import_result',
        'schema_version': 1,
        'dry_run': dry_run,
        'associations': [a.model_dump() for a in document.associations],
        'snapshots': [s.model_dump() for s in document.snapshots],
    }


def _matches(association, snapshot):
    if (snapshot.association_id, snapshot.provider, snapshot.provider_company_id) != (
        association.association_id,
        association.provider,
        association.provider_company_id,
    ) or snapshot.acquired_at < association.verified_at:
        raise ValueError('company snapshot does not match its verified association')


def capture_company_evidence(
    database: Path,
    association_ids: tuple[str, ...],
    *,
    settings: Settings,
    clock: Callable[[], datetime] = now,
    observed_at: datetime | None = None,
    observation_note: str = 'Source observation date unknown',
) -> dict[str, Any]:
    if not association_ids or len(set(association_ids)) != len(association_ids):
        raise ValueError('select unique company association IDs')
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = CompanyEvidenceRepository(store.db)
        associations = tuple(repo.association(key) for key in association_ids)
    outcomes = []
    for association in associations:
        try:
            recorder = CompanyRecorder()
            with company_client(association, recorder, settings=settings) as client:
                if association.provider in ('glassdoor', 'ambitionbox'):
                    payload = client.fetch(association.locator, 0).to_dict()
                else:
                    payload = client.fetch(association.locator, 20).to_dict()
            snapshot = CompanyEvidenceSnapshot(
                snapshot_id=str(uuid4()),
                association_id=association.association_id,
                provider=association.provider,
                provider_company_id=association.provider_company_id,
                acquired_at=utc(clock()),
                observed_at=observed_at,
                observation_note=observation_note,
                payload=payload,
                responses=recorder.completed(),
            )
            import_company_evidence(database, CompanyEvidenceImport(snapshots=(snapshot,)))
            outcomes.append(
                {
                    'association_id': association.association_id,
                    'status': 'captured',
                    'snapshot': snapshot.model_dump(),
                }
            )
        except (SourceError, ValueError, StoreError) as error:
            outcomes.append(
                {
                    'association_id': association.association_id,
                    'status': 'failed',
                    'error': str(error),
                    'error_type': type(error).__name__,
                }
            )
    return {
        'kind': 'company_evidence_capture_result',
        'schema_version': 1,
        'outcomes': outcomes,
        'success': all(o['status'] == 'captured' for o in outcomes),
    }


def show_company_evidence(
    database: Path, *, association_ids: tuple[str, ...] = (), snapshot_ids: tuple[str, ...] = ()
) -> dict[str, Any]:
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = CompanyEvidenceRepository(store.db)
        associations = tuple(repo.association(key) for key in association_ids)
        snapshots = tuple(repo.snapshot(key) for key in snapshot_ids)
    return {
        'kind': 'company_evidence_report',
        'schema_version': 1,
        'associations': [a.model_dump() for a in associations],
        'snapshots': [
            s.model_dump()
            | {'snapshot_hash': s.snapshot_hash, 'observation_date_unknown': s.observed_at is None}
            for s in snapshots
        ],
        'input_digest': content_hash((associations, snapshots)),
    }


def enrich_company_evidence(
    database: Path, document: CompanyEnrichment, *, clock: Callable[[], datetime] = now
) -> JobVersionBundle:
    document = validated(document)
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = CompanyEvidenceRepository(store.db)
        grouped = {}
        for selection in document.selections:
            association = repo.association(selection.association_id)
            snapshot = repo.snapshot(selection.snapshot_id)
            _matches(association, snapshot)
            validate_payload(snapshot)
            parent = JobVersionRepository(store.db).get(association.job_version_id)
            grouped.setdefault(parent.job_version_id, (parent, []))[1].append(
                (association, snapshot)
            )
    jobs = []
    for parent, selected in grouped.values():
        # Multiple provider identities for the same version must be resolved explicitly.
        provider_ids = {}
        for association, _snapshot in selected:
            previous = provider_ids.setdefault(
                association.provider, association.provider_company_id
            )
            if previous != association.provider_company_id:
                raise ValueError('ambiguous company associations for the same provider/job version')
        evidence = list(parent.evidence)
        for association, snapshot in selected:
            acquired = snapshot.acquired_at
            evidence.append(
                Evidence(
                    source=association.provider,
                    observed_at=acquired,
                    extractor_version='company-context-1',
                    payload={
                        'kind': 'company_evidence_context',
                        'schema_version': 1,
                        'parent_job_version_id': parent.job_version_id,
                        'association': association.model_dump(mode='json'),
                        'snapshot_id': snapshot.snapshot_id,
                        'snapshot_hash': snapshot.snapshot_hash,
                        'observed_at': snapshot.observed_at,
                        'observation_note': snapshot.observation_note,
                        'payload': snapshot.payload,
                        'responses': [r.model_dump() for r in snapshot.responses],
                    },
                )
            )
            if (
                snapshot.provider in ('glassdoor', 'ambitionbox')
                and snapshot.observed_at is not None
            ):
                typed, _ = capture_rating(
                    rating_payload(snapshot.payload),
                    verified_company_id=association.provider_company_id,
                    observed_at=snapshot.observed_at,
                )
                evidence.append(typed)
        # Content-addressed ID makes repeating an identical selection retry-safe.
        version = JobVersion(
            parser_version=parent.parser_version,
            job_data=parent.job_data,
            description=parent.description,
            evidence=tuple(evidence),
            captured_at=utc(clock()),
        )
        jobs.append(
            version.model_copy(
                update={
                    'job_version_id': content_hash(
                        {'parent': parent.job_version_id, 'content': version.content_hash}
                    )
                }
            )
        )
    if len({j.job_id for j in jobs}) != len(jobs):
        raise ValueError('enrichment selects multiple parent versions of the same job')
    return JobVersionBundle(kind='ranking_job_versions', schema_version=1, jobs=tuple(jobs))
