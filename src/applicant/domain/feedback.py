"""Explicit candidate judgments, independent of application outcomes."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Identity, Timestamp
from .preferences import DIMENSIONS

REASONS = (
    *DIMENSIONS,
    'location',
    'employment_arrangement',
    'missing_evidence',
    'conflicting_evidence',
    'timing',
    'closed_role',
    'other',
)


class FeedbackInput(Contract):
    kind: Literal['candidate_feedback'] = 'candidate_feedback'
    event_id: Identity
    run_id: Identity
    job_id: Identity
    expected_revision: int = Field(ge=0, strict=True)
    decision: Literal['shortlist', 'dismiss', 'clear']
    reasons: tuple[Identity, ...] = ()
    importance: dict[str, Literal['more_important', 'less_important']] = Field(default_factory=dict)
    notes: str = Field(default='', max_length=2000)

    @model_validator(mode='after')
    def consistent(self):
        if len(set(self.reasons)) != len(self.reasons) or any(
            r not in REASONS for r in self.reasons
        ):
            raise ValueError('reasons must be unique supported reason codes')
        if self.decision == 'dismiss' and not self.reasons:
            raise ValueError('dismissal requires a reason')
        if not self.importance.keys() <= set(DIMENSIONS):
            raise ValueError('importance must name supported dimensions')
        if self.decision == 'clear' and (self.reasons or self.importance or self.notes):
            raise ValueError('clear cannot carry reasons, importance or notes')
        return self


class FeedbackEvent(Contract):
    sequence: int = Field(gt=0, strict=True)
    request: FeedbackInput
    revision: int = Field(gt=0, strict=True)
    job_version_id: Identity
    profile_id: Identity
    profile_revision: int = Field(gt=0, strict=True)
    recorded_at: Timestamp

    @model_validator(mode='after')
    def next_revision(self):
        if self.revision != self.request.expected_revision + 1:
            raise ValueError('feedback revision must follow expected revision')
        return self


class FeedbackDocument(Contract):
    kind: Literal['candidate_feedback_import'] = 'candidate_feedback_import'
    events: tuple[FeedbackInput, ...]
