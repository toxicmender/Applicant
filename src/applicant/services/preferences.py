"""Save candidate edits with optimistic revision checking."""

from __future__ import annotations

from ..domain.preferences import CandidateProfile
from ..infra.store.profiles import ProfileRepository
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.sqlite import Store


def save_profile(
    store: Store, profile: CandidateProfile, *, expected_revision: int
) -> CandidateProfile:
    profile = validated(profile)
    with write_transaction(store.db):
        return ProfileRepository(store.db).append(profile, expected_revision=expected_revision)
