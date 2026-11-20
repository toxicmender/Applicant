"""Explicit capture/import and offline enrichment; ranking never refreshes sources."""

from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..domain.canonical import content_hash, utc
from ..domain.company_evidence import (
    CompanyEnrichment,
    CompanyEvidenceImport,
    CompanyEvidenceSnapshot,
)
from ..domain.company_lineage import enrichment_steps
from ..domain.contracts import now
from ..domain.ranking import Evidence, JobVersion
from ..domain.ranking_inputs import JobVersionBundle
from ..errors import SourceError, StoreError
from ..financials.models import CompanyFinancials, FundingRound, Money
from ..infra.company_sources import CompanyRecorder, company_client
from ..infra.company_worker import CompanyFetchInterrupted, company_session, fetch_company
from ..infra.owned_worker import WorkerFailure
from ..infra.store.company_evidence import CompanyEvidenceRepository
from ..infra.store.job_versions import JobVersionRepository
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.ranking_runs import RankingRunRepository
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


def _acquire_evidence(database, association, settings, clock, observed_at, observation_note):
    from ..financials import CrunchbaseClient, TracxnClient
    from ..reviews import GlassdoorClient
    from .provider_health import Acquisition

    recorder = CompanyRecorder()
    with company_client(association, recorder, settings=settings) as client:
        owned = isinstance(client, GlassdoorClient) or (
            isinstance(client, (CrunchbaseClient, TracxnClient)) and not client.api_key
        )
        access = company_session(association.provider, client) if owned else nullcontext(None)
        with access as state, Acquisition(database, association.provider, clock=clock) as owner:
            if owner.result['status'] != 'acquired':
                return {'association_id': association.association_id, **owner.result}

            def capture(limit):
                if owned:
                    return fetch_company(
                        association.provider,
                        client,
                        association.locator,
                        limit,
                        state=state,
                        check=owner.check,
                        capture=recorder,
                    )
                return client.fetch(association.locator, limit)

            return _commit_capture(
                database,
                association,
                clock,
                observed_at,
                observation_note,
                owner,
                recorder,
                capture,
            )


def _commit_capture(
    database, association, clock, observed_at, observation_note, owner, recorder, capture
):
    from ..errors import AuthFailed, Blocked, Unparseable
    from ..infra.owned_worker import defer_interrupt

    interrupt = None
    try:
        limit = 0 if association.provider in ('glassdoor', 'ambitionbox') else 20
        try:
            result = capture(limit)
        except CompanyFetchInterrupted as error:
            interrupt, result = error, error.result
        owner.check()
        snapshot = CompanyEvidenceSnapshot(
            snapshot_id=str(uuid4()),
            association_id=association.association_id,
            provider=association.provider,
            provider_company_id=association.provider_company_id,
            acquired_at=utc(clock()),
            observed_at=observed_at,
            observation_note=observation_note,
            payload=result.to_dict(),
            responses=recorder.completed(),
        )
        _matches(association, snapshot)
        validate_payload(snapshot)
    except WorkerFailure:
        raise
    except SourceError as error:
        status = (
            'needs_setup'
            if isinstance(error, AuthFailed)
            else 'adapter_degraded'
            if isinstance(error, Unparseable)
            else 'throttled'
            if getattr(error, 'retry_at', None) is not None
            else 'blocked'
            if isinstance(error, Blocked)
            else 'unavailable'
        )
        owner.finish(status, retry_at=getattr(error, 'retry_at', None))
        raise
    with defer_interrupt():
        import_company_evidence(database, CompanyEvidenceImport(snapshots=(snapshot,)))
        owner.finish('success')
    if interrupt is not None:
        raise interrupt
    return {
        'association_id': association.association_id,
        'status': 'captured',
        'snapshot': snapshot.model_dump(),
    }


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
    from .provider_health import LeaseFailure

    outcomes = []
    for association in associations:
        try:
            outcomes.append(
                _acquire_evidence(
                    database, association, settings, clock, observed_at, observation_note
                )
            )
        except (SourceError, ValueError, StoreError, LeaseFailure) as error:
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
    database: Path,
    document: CompanyEnrichment,
    *,
    base_run: str | None = None,
    clock: Callable[[], datetime] = now,
) -> JobVersionBundle:
    document = validated(document)
    with Store.open_readonly(database) as store, store.read_snapshot():
        repo = CompanyEvidenceRepository(store.db)
        versions = JobVersionRepository(store.db)
        parents = ()
        if base_run is not None:
            run = RankingRunRepository(store.db).get(base_run)
            parents = tuple(versions.get(key) for key in run.job_version_ids)
        grouped = {}
        for selection in document.selections:
            association = repo.association(selection.association_id)
            snapshot = repo.snapshot(selection.snapshot_id)
            _matches(association, snapshot)
            validate_payload(snapshot)
            parent = versions.get(association.job_version_id)
            if base_run is not None and parent.job_version_id not in run.job_version_ids:
                raise ValueError(
                    'company association must reference an exact parent in the base run'
                )
            grouped.setdefault(parent.job_version_id, (parent, []))[1].append(
                (association, snapshot)
            )
    jobs = []
    for parent, selected in grouped.values():
        if base_run is not None:
            selected.sort(key=lambda pair: (pair[0].association_id, pair[1].snapshot_id))
        # Multiple provider identities for the same version must be resolved explicitly.
        provider_ids = {}
        for association, _snapshot in selected:
            previous = provider_ids.setdefault(
                association.provider, association.provider_company_id
            )
            if previous != association.provider_company_id:
                raise ValueError('ambiguous company associations for the same provider/job version')
        evidence = list(parent.evidence)
        if base_run is not None:
            steps = enrichment_steps(parent)
            evidence.append(
                Evidence(
                    source='company-evidence',
                    observed_at=max(snapshot.acquired_at for _, snapshot in selected),
                    extractor_version='company-lineage-1',
                    payload={
                        'kind': 'company_enrichment_lineage',
                        'schema_version': 1,
                        'immediate_parent_job_version_id': parent.job_version_id,
                        'enrichment_steps': [
                            *steps,
                            {
                                'parent_job_version_id': parent.job_version_id,
                                'snapshots': [
                                    {
                                        'snapshot_id': snapshot.snapshot_id,
                                        'snapshot_hash': snapshot.snapshot_hash,
                                    }
                                    for _, snapshot in selected
                                ],
                            },
                        ],
                    },
                )
            )
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
    if base_run is not None:
        children = {job.job_id: job for job in jobs}
        jobs = [children.get(parent.job_id, parent) for parent in parents]
    if len({j.job_id for j in jobs}) != len(jobs):
        raise ValueError('enrichment selects multiple parent versions of the same job')
    return JobVersionBundle(kind='ranking_job_versions', schema_version=1, jobs=tuple(jobs))
