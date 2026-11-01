"""Immutable posting content, distinct from mutable listing collections."""

from __future__ import annotations

import sqlite3

from ...domain.canonical import canonical_json
from ...domain.ranking import JobVersion
from ...errors import StoreError
from .ranking_common import decode, require_transaction, validated


class JobVersionRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, version_id: str) -> JobVersion:
        row = self.db.execute(
            'SELECT job_id, content_hash, ranking_hash, payload FROM job_versions WHERE job_version_id = ?',
            (version_id,),
        ).fetchone()
        if row is None:
            raise StoreError(f'job version not found: {version_id}')
        value = decode(JobVersion, row[3])
        if (
            value.job_version_id != version_id
            or (value.job_id, value.content_hash, value.ranking_hash) != row[:3]
        ):
            raise StoreError('job version integrity mismatch')
        return value

    def put(self, version: JobVersion) -> JobVersion:
        require_transaction(self.db)
        version = validated(version)
        self.db.execute(
            'INSERT INTO job_versions VALUES (?, ?, ?, ?, ?) ON CONFLICT(job_id, content_hash) DO NOTHING',
            (
                version.job_version_id,
                version.job_id,
                version.content_hash,
                version.ranking_hash,
                canonical_json(version),
            ),
        )
        row = self.db.execute(
            'SELECT job_version_id FROM job_versions WHERE job_id = ? AND content_hash = ?',
            (version.job_id, version.content_hash),
        ).fetchone()
        assert row is not None
        return self.get(row[0])
