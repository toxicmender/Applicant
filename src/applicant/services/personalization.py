"""Explicit bounded suggestions and atomic profile adoption."""

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..domain.canonical import canonical_json
from ..domain.personalization import propose_weights
from ..domain.ranking import RankingRun
from ..domain.survey import PreferenceDocument
from ..infra.store.feedback import FeedbackRepository
from ..infra.store.personalization import SuggestionRepository
from ..infra.store.profiles import ProfileRepository, RevisionConflict
from ..infra.store.ranking_common import write_transaction
from ..infra.store.sqlite import Store
from .ranking_history import load_history
from .survey import editable_profile


def suggest_weights(database: Path, run_id: str, *, created_at: datetime) -> dict[str, Any]:
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        history = load_history(reader, run_id)
        if isinstance(history.run, RankingRun):
            raise ValueError('suggestions require a modern engine run')
        current = ProfileRepository(reader.db).get(history.profile.profile_id)
        if current.profile_revision != history.profile.profile_revision:
            raise RevisionConflict('run profile revision is no longer current')
        events = FeedbackRepository(reader.db).history(run_id)
        proposed = PreferenceDocument.model_validate(
            editable_profile(history.profile).model_dump()
        ).to_profile(updated_at=created_at)
        suggestion = propose_weights(
            history, events, proposed, suggestion_id=str(uuid4()), created_at=created_at
        )
    if suggestion is None:
        return {
            'kind': 'weight_suggestion_result',
            'schema_version': 1,
            'status': 'insufficient_feedback',
            'reason': 'Need net support from at least three eligible known-score jobs for both increasing and decreasing dimensions.',
        }
    with Store(database) as writer, write_transaction(writer.db):
        if (
            ProfileRepository(writer.db).get(current.profile_id).profile_revision
            != current.profile_revision
        ):
            raise RevisionConflict('profile changed during suggestion generation')
        SuggestionRepository(writer.db).put(suggestion)
    return {
        'kind': 'weight_suggestion_result',
        'schema_version': 1,
        'status': 'proposed',
        'suggestion': suggestion.model_dump(),
    }


def show_suggestion(database: Path, suggestion_id: str):
    with Store.open_readonly(database) as reader, reader.read_snapshot():
        return SuggestionRepository(reader.db).get(suggestion_id)


def accept_suggestion(database: Path, suggestion_id: str, *, accepted_at: datetime):
    # Validate existence before opening a writer; all concurrency checks happen inside it.
    show_suggestion(database, suggestion_id)
    with Store(database) as writer, write_transaction(writer.db):
        suggestion = SuggestionRepository(writer.db).get(suggestion_id)
        profiles = ProfileRepository(writer.db)
        prior = writer.db.execute(
            'SELECT profile_id,profile_revision FROM suggestion_acceptances WHERE suggestion_id=?',
            (suggestion_id,),
        ).fetchone()
        if prior:
            profile = profiles.get(*prior)
            if (profile.profile_id, profile.profile_revision) != (
                suggestion.profile_id,
                suggestion.base_revision + 1,
            ):
                from ..errors import StoreError

                raise StoreError('suggestion acceptance does not match proposed revision')
            return profile
        current = profiles.get(suggestion.profile_id)
        events = FeedbackRepository(writer.db).history(suggestion.run_id)
        if (
            current.profile_revision != suggestion.base_revision
            or max((e.sequence for e in events), default=0) != suggestion.feedback_through
        ):
            raise RevisionConflict(
                'suggestion is stale; regenerate after profile or feedback changes'
            )
        proposed = type(suggestion.proposed_profile).model_validate_json(
            canonical_json(
                suggestion.proposed_profile.model_copy(update={'updated_at': accepted_at})
            )
        )
        saved = profiles.append(proposed, expected_revision=suggestion.base_revision)
        writer.db.execute(
            'INSERT INTO suggestion_acceptances VALUES (?,?,?)',
            (suggestion_id, saved.profile_id, saved.profile_revision),
        )
        return saved
