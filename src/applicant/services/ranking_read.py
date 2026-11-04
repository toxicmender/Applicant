"""Read-only ranking presentation and verification from exact archived inputs."""

from pathlib import Path
from typing import Any

from ..domain.ranking import RankingRun
from ..domain.ranking_inputs import ProvenanceDocument
from ..domain.ranking_report import ranking_report
from ..domain.ranking_v2 import RankingRunV3
from ..errors import StoreError
from ..infra.store.ranking_runs import RankingRunNotFound, RankingRunRepository
from ..infra.store.sqlite import Store
from .ranking_history import load_history
from .ranking_replay import replay_run


class RankingJobNotFound(StoreError):
    pass


def _exists(database: Path) -> bool:
    try:
        database.stat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise StoreError(f'could not read {database}: {error}') from error
    return True


def list_runs(database: Path, *, profile_id: str | None = None) -> dict[str, Any]:
    runs = ()
    if _exists(database):
        with Store.open_readonly(database) as store, store.read_snapshot():
            runs = RankingRunRepository(store.db).completed(profile_id=profile_id)
    return {
        'kind': 'ranking_run_list',
        'schema_version': 1,
        'runs': [
            {
                'ranking_run_id': r.ranking_run_id,
                'profile_id': r.profile_id,
                'profile_revision': r.profile_revision,
                'calculated_at': r.calculated_at,
                'run_schema_version': r.schema_version,
                'job_count': len(r.job_version_ids),
            }
            for r in runs
        ],
    }


def read_run(
    database: Path,
    run_id: str,
    *,
    top: int | None = 20,
    job_id: str | None = None,
    provenance: ProvenanceDocument | None = None,
) -> dict[str, Any]:
    if not _exists(database):
        raise RankingRunNotFound(f'completed ranking run not found: {run_id}')
    with Store.open_readonly(database) as store, store.read_snapshot():
        if provenance is not None:
            verified = replay_run(
                store,
                run_id,
                build_identity=provenance.build_identity,
                lockfile_digest=provenance.lockfile_digest,
            )
            return {
                'kind': 'ranking_replay_report',
                'schema_version': 1,
                'verification': verified.model_dump(),
            }
        history = load_history(store, run_id)
    if job_id is None:
        try:
            return ranking_report(history.run, history.jobs, config=history.config, top=top)
        except ValueError as error:
            raise StoreError(f'invalid archived ranking inputs: {error}') from error
    job = next((j for j in history.jobs if j.job_id == job_id), None)
    if job is None:
        raise RankingJobNotFound(f'job not in run {run_id}: {job_id}')
    result = next((r for r in history.run.results if r.job_id == job_id), None)
    return {
        'kind': 'ranking_explanation',
        'schema_version': 1,
        'run': history.run.model_dump(exclude={'results'}),
        'eligibility': (
            None
            if isinstance(history.run, RankingRun)
            else next(r.eligibility for r in history.run.results if r.job_id == job_id)
        ),
        'format_notice': 'eligibility unavailable in this format'
        if isinstance(history.run, RankingRun)
        else None,
        'result': None if result is None else result.model_dump(),
        'selected_tier': history.run.selected_tiers.get(job_id)
        if isinstance(history.run, RankingRunV3)
        else None,
        'job': job.model_dump(),
        'profile': history.profile.model_dump(),
        'configuration': history.config.model_dump(),
        'fx': history.fx.model_dump(),
    }
