"""Bounded explicit preference adjustments over frozen dimension scores."""

from decimal import Decimal, localcontext
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Identity, Timestamp
from .feedback import FeedbackEvent
from .preferences import DIMENSIONS
from .ranking_math import arithmetic_context
from .ranking_v2 import CandidateProfileV2, JobRankingV2
from .ranking_views import ranked_views
from .scoring import aggregate_dimensions


class WeightSuggestion(Contract):
    suggestion_id: Identity
    policy: Literal['bounded-transfer-v1'] = 'bounded-transfer-v1'
    run_id: Identity
    profile_id: Identity
    base_revision: int = Field(gt=0, strict=True)
    feedback_through: int = Field(ge=0, strict=True)
    supporting_event_ids: tuple[Identity, ...]
    created_at: Timestamp
    proposed_profile: CandidateProfileV2
    recipient: Identity
    donor: Identity
    transfer: Decimal = Field(gt=0, le=Decimal('.05'), allow_inf_nan=False)
    preview: tuple[dict, ...]
    preview_groups: dict

    @model_validator(mode='after')
    def consistent(self):
        if (
            self.proposed_profile.profile_id != self.profile_id
            or self.proposed_profile.profile_revision != self.base_revision + 1
            or self.donor not in DIMENSIONS
            or self.recipient not in DIMENSIONS
            or self.donor == self.recipient
            or not self.supporting_event_ids
            or len(set(self.supporting_event_ids)) != len(self.supporting_event_ids)
        ):
            raise ValueError(
                'suggestion must identify distinct dimensions, supporting events and the next profile revision'
            )
        return self


def propose_weights(
    history,
    events: tuple[FeedbackEvent, ...],
    proposed: CandidateProfileV2,
    *,
    suggestion_id: str,
    created_at,
) -> WeightSuggestion | None:
    profile, run = history.profile, history.run
    scores = {r.job_id: r for r in run.results if r.eligibility == 'eligible'}
    effective = {e.request.job_id: e for e in events}
    support: dict[str, int] = dict.fromkeys(DIMENSIONS, 0)
    used = []
    for job_id, event in effective.items():
        result = scores.get(job_id)
        if result is None:
            continue
        known = {d.dimension for d in result.dimensions if d.status == 'known'}
        relevant = False
        for dimension, direction in event.request.importance.items():
            if dimension in known and profile.weights[dimension] > 0:
                support[dimension] += 1 if direction == 'more_important' else -1
                relevant = True
        if relevant:
            used.append(event.request.event_id)
    recipient = max(DIMENSIONS, key=lambda d: support[d])
    donor = min(DIMENSIONS, key=lambda d: support[d])
    if support[recipient] < 3 or support[donor] > -3:
        return None
    with localcontext(arithmetic_context()):
        transfer = min(Decimal('.05'), profile.weights[donor] / 2, 1 - profile.weights[recipient])
        weights = dict(profile.weights)
        weights[recipient] += transfer
        weights[donor] -= transfer
        proposed = CandidateProfileV2.model_validate(proposed.model_dump() | {'weights': weights})
        previews = []
        for result in run.results:
            if result.eligibility == 'excluded':
                previews.append(result.model_dump())
                continue
            summary = aggregate_dimensions(proposed, result.dimensions, config=history.config)
            previews.append(
                result.model_dump()
                | {
                    'dimensions': summary.dimensions,
                    'overall_score': summary.overall_score,
                    'confidence': summary.confidence,
                    'coverage': summary.coverage,
                }
            )
    return WeightSuggestion(
        suggestion_id=suggestion_id,
        run_id=run.ranking_run_id,
        profile_id=profile.profile_id,
        base_revision=profile.profile_revision,
        feedback_through=max((e.sequence for e in events), default=0),
        supporting_event_ids=tuple(sorted(used)),
        created_at=created_at,
        proposed_profile=proposed,
        recipient=recipient,
        donor=donor,
        transfer=transfer,
        preview=tuple(previews),
        preview_groups=ranked_views(
            tuple(JobRankingV2.model_validate(p) for p in previews), config=history.config
        ).model_dump(),
    )
