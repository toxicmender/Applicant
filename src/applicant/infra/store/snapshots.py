"""Exact FX retrieval and content-deduplicated scoring configuration records."""

from __future__ import annotations

import sqlite3

from ...domain.canonical import canonical_json
from ...domain.fx import FXSnapshot
from ...domain.ranking import ScoringConfiguration
from ...errors import StoreError
from .ranking_common import decode, require_transaction, validated


class FXSnapshotRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, snapshot_id: str) -> FXSnapshot:
        row = self.db.execute(
            'SELECT payload FROM fx_snapshots WHERE snapshot_id = ?', (snapshot_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f'FX snapshot not found: {snapshot_id}')
        value = decode(FXSnapshot, row[0])
        if value.snapshot_id != snapshot_id:
            raise StoreError('FX snapshot identity mismatch')
        return value

    def put(self, snapshot: FXSnapshot) -> FXSnapshot:
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


class ScoringConfigRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, config_id: str) -> ScoringConfiguration:
        row = self.db.execute(
            'SELECT config_hash, payload FROM scoring_configs WHERE scoring_config_id = ?',
            (config_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f'scoring configuration not found: {config_id}')
        value = decode(ScoringConfiguration, row[1])
        if value.scoring_config_id != config_id or value.config_hash != row[0]:
            raise StoreError('scoring configuration integrity mismatch')
        return value

    def put(self, config: ScoringConfiguration) -> ScoringConfiguration:
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
