"""Immutable suggestion and acceptance audit storage."""

from ...domain.canonical import canonical_json
from ...domain.personalization import WeightSuggestion
from ...errors import StoreError
from .ranking_common import decode, require_transaction, validated

PERSONALIZATION_MIGRATION = (
    """CREATE TABLE weight_suggestions (
        suggestion_id TEXT PRIMARY KEY NOT NULL,
        run_id TEXT NOT NULL REFERENCES run_completions(ranking_run_id),
        profile_id TEXT NOT NULL, base_revision INTEGER NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)),
        FOREIGN KEY(profile_id,base_revision) REFERENCES candidate_profiles(profile_id,profile_revision))""",
    """CREATE TABLE suggestion_acceptances (
        suggestion_id TEXT PRIMARY KEY NOT NULL REFERENCES weight_suggestions(suggestion_id),
        profile_id TEXT NOT NULL, profile_revision INTEGER NOT NULL,
        FOREIGN KEY(profile_id,profile_revision) REFERENCES candidate_profiles(profile_id,profile_revision))""",
    *(
        f'CREATE TRIGGER immutable_{table}_{op.lower()} BEFORE {op} ON {table} '
        "BEGIN SELECT RAISE(ABORT, 'immutable personalization history'); END"
        for table in ('weight_suggestions', 'suggestion_acceptances')
        for op in ('UPDATE', 'DELETE')
    ),
)


class SuggestionRepository:
    def __init__(self, db):
        self.db = db

    def get(self, suggestion_id: str) -> WeightSuggestion:
        row = self.db.execute(
            'SELECT run_id,profile_id,base_revision,payload FROM weight_suggestions WHERE suggestion_id=?',
            (suggestion_id,),
        ).fetchone()
        if row is None:
            raise SuggestionNotFound(f'suggestion not found: {suggestion_id}')
        value = decode(WeightSuggestion, row[3])
        if (value.suggestion_id, value.run_id, value.profile_id, value.base_revision) != (
            suggestion_id,
            *row[:3],
        ):
            raise StoreError('suggestion identity does not match stored keys')
        return value

    def put(self, suggestion: WeightSuggestion) -> WeightSuggestion:
        require_transaction(self.db)
        suggestion = validated(suggestion)
        self.db.execute(
            'INSERT INTO weight_suggestions VALUES (?,?,?,?,?)',
            (
                suggestion.suggestion_id,
                suggestion.run_id,
                suggestion.profile_id,
                suggestion.base_revision,
                canonical_json(suggestion),
            ),
        )
        return suggestion


class SuggestionNotFound(StoreError):
    pass
