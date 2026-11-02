"""Offline evaluation and comparison of exact historical engine inputs."""

from ..domain.canonical import canonical_json
from ..domain.contracts import Identity
from ..domain.ranking_engine import evaluate_jobs
from ..domain.ranking_v2 import EngineContract, JobRankingV2, RankingRunV3, ScoringConfigurationV2
from ..errors import StoreError
from ..infra.store.sqlite import Store
from .ranking_history import load_history


class UnsupportedAlgorithm(StoreError):
    pass


class ReplayEnvironmentMismatch(StoreError):
    pass


class ReplayVerification(EngineContract):
    ranking_run_id: Identity
    matches: bool
    mismatched_job_version_ids: tuple[Identity, ...]
    computed_results: tuple[JobRankingV2, ...]


def replay_run(
    store: Store, run_id: str, *, build_identity: str, lockfile_digest: str
) -> ReplayVerification:
    history = load_history(store, run_id)
    run = history.run
    if not isinstance(run, RankingRunV3) or not isinstance(history.config, ScoringConfigurationV2):
        raise UnsupportedAlgorithm(
            'unsupported_algorithm: run is not a frozen-inputs-v1 engine run'
        )
    if run.build_identity != build_identity or run.lockfile_digest != lockfile_digest:
        raise ReplayEnvironmentMismatch('replay build/lock identity differs from the original run')
    computed = evaluate_jobs(
        history.profile,
        history.jobs,
        fx=history.fx,
        config=history.config,
        evaluated_at=run.calculated_at,
        selected_tiers=run.selected_tiers,
    )
    mismatches = tuple(
        original.job_version_id
        for original, actual in zip(run.results, computed, strict=True)
        if canonical_json(original) != canonical_json(actual)
    )
    return ReplayVerification(
        ranking_run_id=run.ranking_run_id,
        matches=not mismatches,
        mismatched_job_version_ids=mismatches,
        computed_results=computed,
    )
