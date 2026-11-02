"""Deterministic views by eligibility, using displayed Decimal scores and stable IDs."""

from decimal import Decimal, localcontext

from .canonical import canonical_json
from .ranking_math import arithmetic_context
from .ranking_v2 import EngineContract, JobRankingV2, ScoringConfigurationV2


class RankedViews(EngineContract):
    eligible: tuple[JobRankingV2, ...]
    review_required: tuple[JobRankingV2, ...]
    excluded: tuple[JobRankingV2, ...]


def ranked_views(
    results: tuple[JobRankingV2, ...], *, config: ScoringConfigurationV2
) -> RankedViews:
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    results = tuple(JobRankingV2.model_validate_json(canonical_json(r)) for r in results)
    if len({r.job_id for r in results}) != len(results):
        raise ValueError('duplicate job identity in ranked views')
    groups = {'eligible': [], 'review_required': [], 'excluded': []}
    with localcontext(arithmetic_context()):

        def key(result):
            score = result.overall_score
            return (
                score is None,
                Decimal(0)
                if score is None
                else score.quantize(Decimal(config.score_quantum)).copy_negate(),
                result.job_id,
            )

        for result in results:
            groups[result.eligibility].append(result)
        return RankedViews(
            eligible=tuple(sorted(groups['eligible'], key=key)),
            review_required=tuple(sorted(groups['review_required'], key=key)),
            excluded=tuple(sorted(groups['excluded'], key=lambda r: r.job_id)),
        )
