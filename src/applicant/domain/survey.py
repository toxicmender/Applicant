"""Editable candidate documents, separate from immutable archived profiles."""

from datetime import datetime, timezone
from decimal import localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import Identity
from .ranking_math import arithmetic_context
from .ranking_v2 import CandidateProfileV2

PROFILE_METADATA = frozenset(
    {
        'schema_version',
        'currency_registry',
        'country_registry',
        'profile_id',
        'profile_revision',
        'survey_version',
        'updated_at',
    }
)
PREFERENCE_FIELDS = frozenset(CandidateProfileV2.model_fields) - PROFILE_METADATA
_VALIDATION_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)


class PreferenceDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')

    kind: Literal['candidate_preferences'] = 'candidate_preferences'
    schema_version: Literal[1] = 1
    profile_id: Identity
    expected_revision: int = Field(ge=0, strict=True)
    preferences: dict[str, Any]

    @model_validator(mode='after')
    def valid_preferences(self):
        extra = self.preferences.keys() - PREFERENCE_FIELDS
        if extra:
            raise ValueError(f'unsupported preference fields: {", ".join(sorted(extra))}')
        self.to_profile(updated_at=_VALIDATION_TIME)
        return self

    def to_profile(self, *, updated_at: datetime) -> CandidateProfileV2:
        with localcontext(arithmetic_context()):
            return CandidateProfileV2(
                **self.preferences,
                profile_id=self.profile_id,
                profile_revision=self.expected_revision + 1,
                updated_at=updated_at,
            )

    def expanded(self) -> dict[str, Any]:
        """All editable defaults, with no generated archival metadata."""
        return self.to_profile(updated_at=_VALIDATION_TIME).model_dump(
            exclude=set(PROFILE_METADATA)
        )
