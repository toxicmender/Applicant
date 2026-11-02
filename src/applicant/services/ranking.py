"""Prepare frozen inputs, resolve FX and atomically complete an evaluated ranking run."""

from datetime import datetime
from uuid import uuid4

from ..domain.canonical import canonical_json, content_hash, utc
from ..domain.ranking import Evidence, JobVersion
from ..domain.ranking_engine import captured_facts, evaluate_jobs
from ..domain.ranking_facts import EmploymentOptions, FactSource, RankingFacts, WorkRoute
from ..domain.ranking_salary import parse_compensation
from ..domain.ranking_v2 import RankingRunV3, ScoringConfigurationV2
from ..errors import StoreError
from ..infra.store.profiles import ProfileRepository
from ..infra.store.ranking_codecs import FX
from ..infra.store.ranking_common import validated
from ..infra.store.sqlite import Store
from .ranking_fx import FXProvider, resolve_fx
from .ranking_history import capture_jobs, complete_run, save_config


def prepare_job(version: JobVersion, *, evaluated_at: datetime) -> JobVersion:
    version = validated(version)
    at = utc(evaluated_at)
    if version.captured_at > at:
        raise ValueError('job capture is after evaluation time')
    bundles, _ = captured_facts(version)
    source = FactSource(
        evidence_id=content_hash({'job_id': version.job_id, 'parser': 'job-facts-1'}),
        source=version.job_data['source'],
        observed_at=version.captured_at,
        extractor_version='job-facts-1',
        quality='structured',
        excerpt='Captured posting fields.',
    )
    evidence = list(version.evidence)
    salaries = ()
    routes = ()
    employment = ()
    parsed_already = any(
        e.payload.get('kind') == 'ranking_salary_parse' and e.payload.get('schema_version', 1) == 1
        for e in version.evidence
    )
    if (
        not any(b.compensation for b in bundles)
        and not parsed_already
        and version.job_data.get('salary')
    ):
        salary_source = FactSource.model_validate(
            source.model_dump()
            | {
                'evidence_id': content_hash({'job_id': version.job_id, 'field': 'salary'}),
                'extractor_version': 'ranking-salary-1',
                'excerpt': version.job_data['salary'].strip() or 'Salary not stated.',
            }
        )
        parsed = parse_compensation(version.job_data['salary'], source=salary_source)
        salaries = parsed.facts
        evidence.append(
            Evidence(
                source=source.source,
                observed_at=source.observed_at,
                extractor_version=parsed.extractor_policy,
                payload=parsed.model_dump(mode='json') | {'kind': 'ranking_salary_parse'},
            )
        )
    if (
        not any(b.routes or b.routes_complete for b in bundles)
        and version.job_data.get('remote') is True
    ):
        inferred = FactSource.model_validate(
            source.model_dump()
            | {
                'quality': 'inferred',
                'evidence_id': content_hash({'job_id': version.job_id, 'field': 'remote'}),
                'excerpt': 'remote=True; geographic permission was not stated.',
            }
        )
        routes = (WorkRoute(route_id='legacy-remote-hint', source=inferred, mode='Remote'),)
    if not any(b.employment for b in bundles):
        value = (version.job_data.get('employment_type') or '').strip().casefold()
        arrangement = {
            'permanent': 'permanent',
            'contract': 'contract',
            'contractor': 'contract',
        }.get(value)
        if arrangement:
            employment_source = FactSource.model_validate(
                source.model_dump()
                | {
                    'evidence_id': content_hash({'job_id': version.job_id, 'field': 'employment'}),
                    'excerpt': version.job_data['employment_type'],
                }
            )
            employment = (
                EmploymentOptions.model_validate(
                    {'source': employment_source, 'arrangements': (arrangement,), 'complete': True}
                ),
            )
    if salaries or routes or employment:
        bundle = RankingFacts(compensation=salaries, routes=routes, employment=employment)
        evidence.append(
            Evidence(
                source=source.source,
                observed_at=source.observed_at,
                extractor_version='job-facts-1',
                payload=bundle.model_dump(mode='json'),
            )
        )
    if tuple(evidence) == version.evidence:
        return version
    return JobVersion.model_validate(
        version.model_dump() | {'job_version_id': str(uuid4()), 'evidence': tuple(evidence)}
    )


def rank_jobs(
    store: Store,
    *,
    profile_id: str,
    profile_revision: int,
    jobs: tuple[JobVersion, ...],
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    build_identity: str,
    lockfile_digest: str,
    selected_tiers: dict[str, str] | None = None,
    offline: bool = False,
    supplied_fx: FX | None = None,
    static_fx: FX | None = None,
    provider: FXProvider | None = None,
) -> RankingRunV3:
    if store.db.in_transaction:
        raise StoreError('ranking requires a connection without an active transaction')
    at = utc(evaluated_at)
    profile = ProfileRepository(store.db).get(profile_id, profile_revision)
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    jobs = tuple(validated(j) for j in jobs)
    if len({j.job_id for j in jobs}) != len(jobs):
        raise ValueError('select exactly one version of each job')
    choices = {} if selected_tiers is None else dict(selected_tiers)
    if not choices.keys() <= {j.job_id for j in jobs}:
        raise ValueError('salary tier selection is not an input job')
    header = RankingRunV3(
        profile_id=profile_id,
        profile_revision=profile_revision,
        scoring_config_id=config.scoring_config_id,
        fx_snapshot_id='pending',
        calculated_at=at,
        build_identity=build_identity,
        lockfile_digest=lockfile_digest,
    )
    # Validate per-job selection strings before any durable write.
    for key, value in choices.items():
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f'invalid salary tier for {key}')
    prepared = tuple(prepare_job(j, evaluated_at=at) for j in jobs)
    saved_config = save_config(store, config)
    assert isinstance(saved_config, ScoringConfigurationV2)
    currencies = {
        f.currency
        for j in prepared
        for b in captured_facts(j)[0]
        for f in b.compensation
        if f.currency is not None
    }
    resolution = resolve_fx(
        store,
        target_currency=profile.salary_currency,
        source_currencies=currencies,
        config=saved_config,
        evaluated_at=at,
        offline=offline,
        supplied=supplied_fx,
        static=static_fx,
        provider=provider,
    )
    versions = capture_jobs(store, prepared)
    results = evaluate_jobs(
        profile,
        versions,
        fx=resolution.snapshot,
        config=saved_config,
        evaluated_at=at,
        selected_tiers=choices,
    )
    run = RankingRunV3.model_validate(
        header.model_dump()
        | {
            'scoring_config_id': saved_config.scoring_config_id,
            'fx_snapshot_id': resolution.snapshot.snapshot_id,
            'job_version_ids': tuple(v.job_version_id for v in versions),
            'results': results,
            'selected_tiers': choices,
            'resolution_diagnostics': resolution.diagnostics,
        }
    )
    saved = complete_run(store, run)
    assert isinstance(saved, RankingRunV3)
    return saved
