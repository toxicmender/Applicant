"""Noninteractive profile workflows with explicit revision ownership."""

from datetime import datetime
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.survey import PROFILE_METADATA, PreferenceDocument
from ..errors import StoreError
from ..infra.store.profiles import ProfileNotFound, ProfileRepository, RevisionConflict
from ..infra.store.ranking_codecs import Profile
from ..infra.store.sqlite import Store
from .preferences import save_profile


def load_profile(database: Path, profile_id: str, revision: int | None = None) -> Profile:
    try:
        database.stat()
    except FileNotFoundError:
        raise ProfileNotFound(f'profile not found: {profile_id}') from None
    except OSError as error:
        raise StoreError(f'could not read {database}: {error}') from error
    with Store.open_readonly(database) as store, store.read_snapshot():
        return ProfileRepository(store.db).get(profile_id, revision)


def editable_profile(profile: Profile) -> PreferenceDocument:
    return PreferenceDocument(
        profile_id=profile.profile_id,
        expected_revision=profile.profile_revision,
        preferences=profile.model_dump(exclude=set(PROFILE_METADATA)),
    )


def import_preferences(
    database: Path, document: PreferenceDocument, *, updated_at: datetime, dry_run: bool = False
) -> Profile:
    document = PreferenceDocument.model_validate_json(canonical_json(document))
    candidate = document.to_profile(updated_at=updated_at)
    if dry_run:
        try:
            current = load_profile(database, document.profile_id).profile_revision
        except ProfileNotFound:
            current = 0
        if current != document.expected_revision:
            raise RevisionConflict(
                f'expected revision {document.expected_revision}; current revision is {current}'
            )
        return candidate
    with Store(database) as store:
        return save_profile(store, candidate, expected_revision=document.expected_revision)
