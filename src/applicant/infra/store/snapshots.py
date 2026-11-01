"""Exact FX retrieval and content-deduplicated scoring configuration records."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from ...domain.canonical import canonical_json, utc
from ...errors import StoreError
from .ranking_codecs import CONFIG_CODEC, FX, FX_CODEC, Config
from .ranking_common import decode_versioned, require_transaction, validated


class FXSnapshotRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, snapshot_id: str) -> FX:
        row = self.db.execute(
            'SELECT payload FROM fx_snapshots WHERE snapshot_id = ?', (snapshot_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f'FX snapshot not found: {snapshot_id}')
        value = decode_versioned(FX_CODEC, row[0], label='FXSnapshot')
        if value.snapshot_id != snapshot_id:
            raise StoreError('FX snapshot identity mismatch')
        return value

    def put(self, snapshot: FX) -> FX:
        require_transaction(self.db)
        snapshot = validated(snapshot)
        self.db.execute(
            'INSERT INTO fx_snapshots VALUES (?, ?) ON CONFLICT(snapshot_id) DO NOTHING',
            (snapshot.snapshot_id, canonical_json(snapshot)),
        )
        existing = self.get(snapshot.snapshot_id)
        if existing != snapshot:
            raise StoreError('FX snapshot ID already contains different data')
        return existing

    def latest_eligible(
        self,
        *,
        evaluated_at: datetime,
        max_age_days: int,
        target_currency: str,
        source_currencies: set[str],
    ) -> FX | None:
        """Select an exact market snapshot; never merge rates across snapshots."""
        evaluated_at = utc(evaluated_at)
        if max_age_days < 0:
            raise ValueError('maximum FX age must be nonnegative')
        candidates = []
        for (snapshot_id,) in self.db.execute('SELECT snapshot_id FROM fx_snapshots'):
            snapshot = self.get(snapshot_id)
            if snapshot.provenance not in {'live', 'cache'}:
                continue
            age = (evaluated_at.date() - snapshot.effective_date).days
            if not 0 <= age <= max_age_days or snapshot.captured_at > evaluated_at:
                continue
            if target_currency not in snapshot.rates:
                continue
            if not (source_currencies - {target_currency}) & snapshot.rates.keys():
                continue
            candidates.append(snapshot)
        candidates.sort(
            key=lambda value: (
                -value.effective_date.toordinal(),
                -value.retrieved_at.timestamp(),
                value.snapshot_id,
            )
        )
        return candidates[0] if candidates else None


class ScoringConfigRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, config_id: str) -> Config:
        row = self.db.execute(
            'SELECT config_hash, payload FROM scoring_configs WHERE scoring_config_id = ?',
            (config_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f'scoring configuration not found: {config_id}')
        value = decode_versioned(CONFIG_CODEC, row[1], label='ScoringConfiguration')
        if value.scoring_config_id != config_id or value.config_hash != row[0]:
            raise StoreError('scoring configuration integrity mismatch')
        return value

    def put(self, config: Config) -> Config:
        require_transaction(self.db)
        config = validated(config)
        self.db.execute(
            'INSERT INTO scoring_configs VALUES (?, ?, ?) ON CONFLICT(config_hash) DO NOTHING',
            (config.scoring_config_id, config.config_hash, canonical_json(config)),
        )
        row = self.db.execute(
            'SELECT scoring_config_id FROM scoring_configs WHERE config_hash = ?',
            (config.config_hash,),
        ).fetchone()
        assert row is not None
        return self.get(row[0])
