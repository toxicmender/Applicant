"""Immutable candidate revisions and an atomically updated head."""

from __future__ import annotations

import sqlite3

from ...domain.canonical import canonical_json
from ...errors import StoreError
from .ranking_codecs import PROFILE_CODEC, Profile
from .ranking_common import decode_versioned, require_transaction, validated


class RevisionConflict(StoreError):
    """The candidate changed since the caller began editing."""


class ProfileRepository:
    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get(self, profile_id: str, revision: int | None = None) -> Profile:
        if revision is None:
            row = self.db.execute(
                'SELECT active_revision FROM profile_heads WHERE profile_id = ?', (profile_id,)
            ).fetchone()
            if row is None:
                raise StoreError(f'profile not found: {profile_id}')
            revision = row[0]
        row = self.db.execute(
            'SELECT payload FROM candidate_profiles WHERE profile_id = ? AND profile_revision = ?',
            (profile_id, revision),
        ).fetchone()
        if row is None:
            raise StoreError(f'profile revision not found: {profile_id}/{revision}')
        value = decode_versioned(PROFILE_CODEC, row[0], label='CandidateProfile')
        if (value.profile_id, value.profile_revision) != (profile_id, revision):
            raise StoreError('profile identity does not match stored key')
        return value

    def append(self, profile: Profile, *, expected_revision: int) -> Profile:
        require_transaction(self.db)
        profile = validated(profile)
        row = self.db.execute(
            'SELECT active_revision FROM profile_heads WHERE profile_id = ?', (profile.profile_id,)
        ).fetchone()
        current = row[0] if row else 0
        if expected_revision != current or profile.profile_revision != current + 1:
            raise RevisionConflict(
                f'expected revision {expected_revision}; current revision is {current}'
            )
        self.db.execute(
            'INSERT INTO candidate_profiles VALUES (?, ?, ?)',
            (profile.profile_id, profile.profile_revision, canonical_json(profile)),
        )
        self.db.execute(
            'INSERT INTO profile_heads VALUES (?, ?) ON CONFLICT(profile_id) DO UPDATE SET active_revision = excluded.active_revision',
            (profile.profile_id, profile.profile_revision),
        )
        return profile
