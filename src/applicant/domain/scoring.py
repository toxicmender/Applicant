"""Known-only fit aggregation, independent evidence confidence and job results."""

from datetime import datetime
from decimal import Decimal, Underflow, localcontext

from .canonical import canonical_json
from .compensation import NormalizedCompensation
from .contracts import Fraction
from .dimension_scoring import score_dimensions
from .eligibility import Profile, validated_profile
from .language_fit import preferred_score
from .preferences import DIMENSIONS, LANGUAGE_DIMENSION
from .ranking_facts import RankingFacts
from .ranking_math import arithmetic_context
from .ranking_v2 import (
    DimensionResult,
    EligibilityDecision,
    EngineContract,
    JobRankingV2,
    JobRankingV3,
    Score,
    ScoringConfigurationV2,
)


class ScoreSummary(EngineContract):
    dimensions: tuple[DimensionResult, ...]
    overall_score: Score | None
    confidence: Fraction
    coverage: Fraction
    notes: tuple[str, ...] = ()


def aggregate_dimensions(
    profile: Profile, dimensions: tuple[DimensionResult, ...], *, config: ScoringConfigurationV2
) -> ScoreSummary:
    profile = validated_profile(profile)
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    dimensions = tuple(DimensionResult.model_validate_json(canonical_json(d)) for d in dimensions)
    names = tuple(profile.weights)
    if (
        LANGUAGE_DIMENSION not in names
        and any(d.dimension == LANGUAGE_DIMENSION for d in dimensions)
    ) or LANGUAGE_DIMENSION in names:
        names = (*DIMENSIONS, LANGUAGE_DIMENSION)
    else:
        names = DIMENSIONS
    if len(dimensions) != len(names) or {d.dimension for d in dimensions} != set(names):
        raise ValueError('aggregation requires each dimension exactly once')
    by_name = {d.dimension: d for d in dimensions}
    with localcontext(arithmetic_context()) as context:
        values = []
        for name in names:
            value = by_name[name]
            if profile.weights.get(name, Decimal(0)) == 0:
                value = DimensionResult(
                    dimension=name, status='disabled', reason_codes=('zero_weight',)
                )
            elif value.status in {'disabled', 'not_evaluated'}:
                raise ValueError('active dimensions must be evaluated before aggregation')
            values.append(value)
        active = [d for d in values if d.status not in {'disabled', 'not_applicable'}]
        known = [d for d in active if d.status == 'known']
        applicable_weight = sum((profile.weights[d.dimension] for d in active), Decimal(0))
        known_weight = sum((profile.weights[d.dimension] for d in known), Decimal(0))
        if not known:
            return ScoreSummary(
                dimensions=tuple(values),
                overall_score=None,
                confidence=Decimal(0),
                coverage=Decimal(0),
                notes=('no_applicable_dimensions' if not active else 'insufficient_evidence',),
            )
        if known_weight == 0 or applicable_weight == 0:
            raise ValueError('weights are outside the supported arithmetic range')
        weighted = []
        for d in values:
            if d.score is not None:
                d = DimensionResult.model_validate(
                    d.model_dump()
                    | {
                        'score': d.score.quantize(Decimal(config.score_quantum)),
                        'weighted_contribution': (
                            profile.weights[d.dimension]
                            * d.score.quantize(Decimal(config.score_quantum))
                            / known_weight
                        ),
                    }
                )
            weighted.append(d)
        score = sum(
            (d.weighted_contribution for d in weighted if d.weighted_contribution is not None),
            Decimal(0),
        )
        confidence = (
            sum((profile.weights[d.dimension] * d.quality for d in known), Decimal(0))
            / applicable_weight
        )
        if context.flags[Underflow]:
            raise ValueError('weights are outside the supported arithmetic range')
        return ScoreSummary(
            dimensions=tuple(weighted),
            overall_score=score.quantize(Decimal(config.score_quantum)),
            coverage=(known_weight / applicable_weight).quantize(
                Decimal(config.confidence_quantum)
            ),
            confidence=confidence.quantize(Decimal(config.confidence_quantum)),
        )


def score_job(
    job_id: str,
    job_version_id: str,
    profile: Profile,
    bundles: tuple[RankingFacts, ...],
    *,
    decision: EligibilityDecision,
    compensation: NormalizedCompensation | None,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    selected_tier: str | None = None,
    language_groups: tuple[dict, ...] | None = None,
) -> JobRankingV2:
    decision = EligibilityDecision.model_validate_json(canonical_json(decision))
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    if compensation is not None:
        compensation = NormalizedCompensation.model_validate_json(canonical_json(compensation))
        if (
            compensation.status == 'available'
            and compensation.scoring_config_id != config.scoring_config_id
        ):
            raise ValueError('normalization references a different scoring configuration')
    result_type = JobRankingV2 if language_groups is None else JobRankingV3
    extra = {} if language_groups is None else {'language_groups': language_groups}
    names = DIMENSIONS if language_groups is None else (*DIMENSIONS, LANGUAGE_DIMENSION)
    if decision.status == 'excluded':
        return result_type.model_validate(
            dict(
                job_id=job_id,
                job_version_id=job_version_id,
                decision=decision,
                compensation=compensation,
                dimensions=tuple(
                    DimensionResult(dimension=d, status='not_evaluated', reason_codes=('excluded',))
                    for d in names
                ),
                **extra,
            )
        )
    dimensions = score_dimensions(
        profile,
        bundles,
        compensation=compensation,
        config=config,
        evaluated_at=evaluated_at,
        selected_tier=selected_tier,
    )
    if language_groups is not None:
        score, quality = preferred_score(language_groups)
        dimensions = (
            *dimensions,
            DimensionResult(
                dimension=LANGUAGE_DIMENSION,
                status='known' if score is not None else 'unknown',
                score=score,
                quality=quality,
            ),
        )
    summary = aggregate_dimensions(profile, dimensions, config=config)
    return result_type.model_validate(
        dict(
            job_id=job_id,
            job_version_id=job_version_id,
            decision=decision,
            dimensions=summary.dimensions,
            compensation=compensation,
            overall_score=summary.overall_score,
            confidence=summary.confidence,
            coverage=summary.coverage,
            **extra,
        )
    )
