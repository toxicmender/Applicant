"""Atomically insert run metadata, ordered membership, results and completion."""

from __future__ import annotations

import json
import sqlite3

from ...domain.canonical import canonical_json
from ...domain.ranking_v2 import RankingRunV2, RankingRunV3, ScoringConfigurationV2
from ...errors import StoreError
from .job_versions import JobVersionRepository
from .profiles import ProfileRepository
from .ranking_codecs import RESULT_CODEC, RUN_CODEC, Run
from .ranking_common import decode_versioned, require_transaction, validated
from .snapshots import FXSnapshotRepository, ScoringConfigRepository


class RankingRunNotFound(StoreError):
    """The requested completed run is absent, rather than corrupt."""


class RankingRunRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def completed(self, *, profile_id: str | None = None) -> tuple[Run, ...]:
        """Validated completed history, newest first with stable ID ties.

        Call within Store.read_snapshot() for consistency across related queries.
        Decode timestamps rather than sorting offset-bearing JSON as text.
        """
        rows = self.db.execute(
            'SELECT r.ranking_run_id FROM ranking_runs r '
            'JOIN run_completions c USING(ranking_run_id)'
            + (' WHERE r.profile_id = ?' if profile_id is not None else ''),
            () if profile_id is None else (profile_id,),
        ).fetchall()
        runs = sorted((self.get(row[0]) for row in rows), key=lambda r: r.ranking_run_id)
        return tuple(sorted(runs, key=lambda r: r.calculated_at, reverse=True))

    def create(self, run: Run) -> Run:
        require_transaction(self.db)
        run = validated(run)
        ProfileRepository(self.db).get(run.profile_id, run.profile_revision)
        FXSnapshotRepository(self.db).get(run.fx_snapshot_id)
        config = ScoringConfigRepository(self.db).get(run.scoring_config_id)
        if isinstance(run, (RankingRunV2, RankingRunV3)) and (
            not isinstance(config, ScoringConfigurationV2)
            or config.algorithm_version != run.algorithm_version
        ):
            raise StoreError('engine run requires matching engine configuration')
        versions = [JobVersionRepository(self.db).get(key) for key in run.job_version_ids]
        self.db.execute(
            'INSERT INTO ranking_runs VALUES (?, ?, ?, ?, ?, ?)',
            (
                run.ranking_run_id,
                run.profile_id,
                run.profile_revision,
                run.fx_snapshot_id,
                run.scoring_config_id,
                canonical_json(run.model_dump(exclude={'job_version_ids', 'results'})),
            ),
        )
        for ordinal, version in enumerate(versions):
            self.db.execute(
                'INSERT INTO run_inputs VALUES (?, ?, ?, ?)',
                (run.ranking_run_id, ordinal, version.job_id, version.job_version_id),
            )
        for result in run.results:
            self.db.execute(
                'INSERT INTO job_rankings VALUES (?, ?, ?, ?, ?, ?)',
                (
                    run.ranking_run_id,
                    result.job_id,
                    result.job_version_id,
                    float(result.overall_score) if result.overall_score is not None else None,
                    float(result.confidence) if result.confidence is not None else None,
                    canonical_json(result),
                ),
            )
        self.db.execute('INSERT INTO run_completions VALUES (?)', (run.ranking_run_id,))
        return self.get(run.ranking_run_id)

    def get(self, run_id: str) -> Run:
        row = self.db.execute(
            'SELECT r.profile_id, r.profile_revision, r.fx_snapshot_id, r.scoring_config_id, r.payload FROM ranking_runs r JOIN run_completions c USING(ranking_run_id) WHERE r.ranking_run_id = ?',
            (run_id,),
        ).fetchone()
        if row is None:
            raise RankingRunNotFound(f'completed ranking run not found: {run_id}')
        inputs = self.db.execute(
            'SELECT job_version_id FROM run_inputs WHERE ranking_run_id = ? ORDER BY ordinal',
            (run_id,),
        ).fetchall()
        results = self.db.execute(
            'SELECT j.job_id, j.job_version_id, j.overall_score, j.confidence, j.payload FROM job_rankings j JOIN run_inputs i USING(ranking_run_id, job_id, job_version_id) WHERE j.ranking_run_id = ? ORDER BY i.ordinal',
            (run_id,),
        ).fetchall()
        supplied = []
        for job_id, version_id, score, confidence, payload in results:
            result = decode_versioned(RESULT_CODEC, payload, label='JobRanking')
            if (
                result.job_id,
                result.job_version_id,
                float(result.overall_score) if result.overall_score is not None else None,
                float(result.confidence) if result.confidence is not None else None,
            ) != (job_id, version_id, score, confidence):
                raise StoreError('ranking result integrity mismatch')
            supplied.append(result.model_dump())
        try:
            data = json.loads(row[4])
            if not isinstance(data, dict):
                raise ValueError('run metadata must be an object')
            data.update(job_version_ids=[item[0] for item in inputs], results=supplied)
            run = decode_versioned(RUN_CODEC, canonical_json(data), label='RankingRun')
        except (TypeError, ValueError) as error:
            raise StoreError('invalid ranking run metadata') from error
        if (
            run.ranking_run_id != run_id
            or (run.profile_id, run.profile_revision, run.fx_snapshot_id, run.scoring_config_id)
            != row[:4]
        ):
            raise StoreError('ranking run identity mismatch')
        return run
