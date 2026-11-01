"""Save candidate edits with optimistic revision checking."""

from __future__ import annotations

from ..infra.store.profiles import ProfileRepository
from ..infra.store.ranking_codecs import Profile
from ..infra.store.ranking_common import validated, write_transaction
from ..infra.store.sqlite import Store


def save_profile(store: Store, profile: Profile, *, expected_revision: int) -> Profile:
    profile = validated(profile)
    with write_transaction(store.db):
        return ProfileRepository(store.db).append(profile, expected_revision=expected_revision)
