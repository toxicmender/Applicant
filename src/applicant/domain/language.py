"""Role language evidence and candidate proficiency contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .canonical import content_hash
from .contracts import Identity

Skill = Literal['overall', 'spoken', 'written']
Strength = Literal['required', 'preferred', 'not_required']
Scale = Literal['cefr', 'named']
LEVELS = {
    'cefr': ('A1', 'A2', 'B1', 'B2', 'C1', 'C2'),
    'named': ('basic', 'conversational', 'professional working', 'fluent'),
}
ALIASES: dict[str, str] = dict(
    zip(
        ('english', 'german', 'french', 'dutch', 'spanish', 'hindi', 'japanese', 'mandarin'),
        ('en', 'de', 'fr', 'nl', 'es', 'hi', 'ja', 'zh'),
        strict=True,
    )
)


def language_key(value: str) -> str:
    normalized = ' '.join(value.casefold().split())
    return ALIASES.get(normalized, normalized)


def singleton(job_version_id: str, clause_id: str) -> str:
    return 'singleton-' + content_hash((job_version_id, clause_id, 'singleton'))


class LanguageRecord(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)


class Threshold(LanguageRecord):
    kind: Literal['no_minimum', 'level', 'raw']
    scale: Scale | None = None
    level: Identity | None = None

    @model_validator(mode='after')
    def validate_level(self):
        if self.kind == 'level' and (self.scale is None or self.level not in LEVELS[self.scale]):
            raise ValueError('level must belong to the declared scale')
        if self.kind == 'no_minimum' and (self.scale is not None or self.level is not None):
            raise ValueError('no_minimum has no scale or level')
        if self.kind == 'raw' and (self.scale is not None or self.level is None):
            raise ValueError('raw threshold requires verbatim wording')
        return self


class LanguageExpression(LanguageRecord):
    operator: Literal['all_of', 'any_of']
    members: tuple[Identity | LanguageExpression, ...]

    @model_validator(mode='after')
    def nonempty(self):
        if not self.members:
            raise ValueError('language expression cannot be empty')
        return self


class LanguageAssertion(LanguageRecord):
    kind: Literal['language'] = 'language'
    clause_id: Identity
    member_id: Identity | None = None
    locator: Identity | None = None
    action: Literal['assert', 'retire'] = 'assert'
    supersedes: tuple[Identity, ...] = ()
    language: Identity | None = None
    skill: Skill | None = None
    strength: Strength | None = None
    threshold: Threshold | None = None
    expression: LanguageExpression | None = None
    interpretation_review: bool = False

    @field_validator('language')
    @classmethod
    def normalize_language(cls, value):
        return language_key(value) if value is not None else None

    @model_validator(mode='after')
    def validate_retirement(self):
        if len(set(self.supersedes)) != len(self.supersedes):
            raise ValueError('duplicate supersession reference')
        if self.action == 'retire' and not self.supersedes:
            raise ValueError('retirement must name an assertion')
        return self


class LanguageAbility(LanguageRecord):
    ability: Literal['none', 'yes'] | None = None
    level: Threshold | None = None
    raw: Identity | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.level is not None and (self.level.kind != 'level' or self.ability == 'none'):
            raise ValueError('a recognized level requires positive proficiency')
        return self


class CandidateLanguage(LanguageRecord):
    language: Identity
    none: bool = False
    overall: LanguageAbility | None = None
    spoken: LanguageAbility | None = None
    written: LanguageAbility | None = None

    @field_validator('language')
    @classmethod
    def normalize_language(cls, value):
        return language_key(value)

    @model_validator(mode='after')
    def consistent(self):
        if self.none and any(
            skill is not None and (skill.ability == 'yes' or skill.level is not None)
            for skill in (self.overall, self.spoken, self.written)
        ):
            raise ValueError('language-wide none contradicts a positive skill')
        return self
