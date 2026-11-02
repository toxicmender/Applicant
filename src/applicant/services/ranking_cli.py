"""Validate and freeze CLI selections before opening a writer or acquiring FX."""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..domain.canonical import utc
from ..domain.contracts import now
from ..domain.ranking import JobVersion
from ..domain.ranking_engine import captured_facts
from ..domain.ranking_inputs import JobVersionBundle, ProvenanceDocument
from ..domain.ranking_v2 import RankingRunV3, ScoringConfigurationV2
from ..errors import ConfigError, StoreError
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.listing_snapshots import listing_snapshot
from ..infra.store.profiles import ProfileNotFound, ProfileRepository
from ..infra.store.sqlite import Store
from .ranking import prepare_job, rank_jobs


class EmptyRankingInput(StoreError):
    """Nothing selected; no writer, network request or run has been created."""


def create_ranking_run(
    database: Path,
    *,
    listing: Path,
    provenance_path: Path,
    versions_path: Path | None = None,
    config_path: Path | None = None,
    tiers_path: Path | None = None,
    profile_id: str = 'default',
    revision: int | None = None,
    offline: bool = False,
    clock: Callable[[], datetime] = now,
) -> RankingRunV3:
    try:
        provenance = ProvenanceDocument.model_validate(read_ranking_document(provenance_path))
        config = (
            ScoringConfigurationV2()
            if config_path is None
            else ScoringConfigurationV2.model_validate(
                read_ranking_document(config_path, decimal=True)
            )
        )
        bundle = (
            None
            if versions_path is None
            else JobVersionBundle.model_validate(read_ranking_document(versions_path))
        )
        tiers = {} if tiers_path is None else read_ranking_document(tiers_path)
        if not isinstance(tiers, dict) or any(
            not isinstance(v, str)
            or not v.strip()
            or v != v.strip()
            or not k.strip()
            or k != k.strip()
            for k, v in tiers.items()
        ):
            raise ValueError('tiers must map exact nonblank job IDs to nonblank tier strings')
        try:
            database.stat()
        except FileNotFoundError:
            raise ConfigError('no candidate profile; run applicant survey first') from None
        except OSError as error:
            raise StoreError(f'could not read {database}: {error}') from error
        at = utc(clock())
        with Store.open_readonly(database) as reader, reader.read_snapshot():
            try:
                profile = ProfileRepository(reader.db).get(profile_id, revision)
            except ProfileNotFound as error:
                raise ConfigError(f'{error}; run applicant survey first') from error
            jobs = (
                bundle.jobs
                if bundle is not None
                else tuple(
                    JobVersion(
                        job_data=j.model_dump(exclude={'flags'}),
                        captured_at=at,
                        parser_version='cli-listing-1',
                    )
                    for j in listing_snapshot(listing, store=reader)
                )
            )
        if not jobs:
            raise EmptyRankingInput('no jobs selected; no ranking run created')
        if len({j.job_id for j in jobs}) != len(jobs):
            raise ValueError('select exactly one version of each job')
        if not tiers.keys() <= {j.job_id for j in jobs}:
            raise ValueError('salary tier selection is not an input job')
        jobs = tuple(prepare_job(j, evaluated_at=at) for j in jobs)
        for job in jobs:
            for evidence in job.evidence:
                if (
                    evidence.payload.get('kind') in {'ranking_facts', 'ranking_salary_parse'}
                    and evidence.observed_at > at
                ):
                    raise ValueError('evidence observation is after evaluation time')
            for facts in captured_facts(job)[0]:
                assertions = (
                    *facts.compensation,
                    *facts.routes,
                    *facts.employment,
                    *facts.technologies,
                    *facts.ratings,
                )
                if any(f.source.observed_at > at for f in assertions):
                    raise ValueError('fact observation is after evaluation time')
    except ValueError as error:
        raise ConfigError(f'invalid ranking inputs: {error}') from error
    with Store(database) as writer:
        return rank_jobs(
            writer,
            profile_id=profile.profile_id,
            profile_revision=profile.profile_revision,
            jobs=jobs,
            config=config,
            evaluated_at=at,
            build_identity=provenance.build_identity,
            lockfile_digest=provenance.lockfile_digest,
            selected_tiers=tiers,
            offline=offline,
        )
