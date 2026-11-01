"""Explicit SQLite history capture and offline recovery of complete run inputs."""

from __future__ import annotations

from ..domain.contracts import Contract
from ..domain.fx import FXSnapshot
from ..domain.preferences import CandidateProfile
from ..domain.ranking import JobVersion, RankingRun, ScoringConfiguration
from ..infra.store.job_versions import JobVersionRepository
from ..infra.store.profiles import ProfileRepository
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.ranking_runs import RankingRunRepository
from ..infra.store.snapshots import FXSnapshotRepository, ScoringConfigRepository
from ..infra.store.sqlite import Store


class RankingHistory(Contract):
    run: RankingRun
    profile: CandidateProfile
    fx: FXSnapshot
    config: ScoringConfiguration
    jobs: tuple[JobVersion, ...]


def save_fx(store: Store, snapshot: FXSnapshot) -> FXSnapshot:
    snapshot = validated(snapshot)
    with write_transaction(store.db):
        return FXSnapshotRepository(store.db).put(snapshot)


def save_config(store: Store, config: ScoringConfiguration) -> ScoringConfiguration:
    config = validated(config)
    with write_transaction(store.db):
        return ScoringConfigRepository(store.db).put(config)


def capture_jobs(store: Store, versions: tuple[JobVersion, ...]) -> tuple[JobVersion, ...]:
    versions = tuple(validated(value) for value in versions)
    with write_transaction(store.db):
        repository = JobVersionRepository(store.db)
        return tuple(repository.put(value) for value in versions)


def complete_run(store: Store, run: RankingRun) -> RankingRun:
    run = validated(run)
    with write_transaction(store.db):
        return RankingRunRepository(store.db).create(run)


def load_history(store: Store, run_id: str) -> RankingHistory:
    run = RankingRunRepository(store.db).get(run_id)
    return RankingHistory(
        run=run,
        profile=ProfileRepository(store.db).get(run.profile_id, run.profile_revision),
        fx=FXSnapshotRepository(store.db).get(run.fx_snapshot_id),
        config=ScoringConfigRepository(store.db).get(run.scoring_config_id),
        jobs=tuple(JobVersionRepository(store.db).get(key) for key in run.job_version_ids),
    )
