"""Six preference utilities from captured facts; no acquisition or mutable lookups."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
from typing import Literal

from .canonical import canonical_json, content_hash, utc
from .compensation import NormalizedCompensation
from .eligibility import Profile, validated_profile
from .preferences import DIMENSIONS
from .ranking_facts import FactSource, RankingFacts
from .ranking_math import arithmetic_context
from .ranking_v2 import DimensionResult, ScoringConfigurationV2


def _empty(
    dimension: str,
    reason: str,
    *,
    status: Literal['unknown', 'not_applicable', 'disabled'] = 'unknown',
) -> DimensionResult:
    return DimensionResult(dimension=dimension, status=status, reason_codes=(reason,))


def _quality(source: FactSource, config: ScoringConfigurationV2, at: datetime) -> Decimal:
    if source.observed_at > at:
        raise ValueError('evidence observation is after evaluation time')
    return config.evidence_weights[source.quality]


def _known(
    dimension: str,
    value: Decimal,
    quality: Decimal,
    reason: str,
    *,
    evidence: tuple[str, ...] = (),
    terms: dict[str, Decimal] | None = None,
) -> DimensionResult:
    return DimensionResult(
        dimension=dimension,
        status='known',
        score=value.quantize(Decimal('.01')),
        quality=quality,
        reason_codes=(reason,),
        evidence_ids=evidence,
        calculation_terms={} if terms is None else terms,
    )


def _compensation(
    profile: Profile,
    pay: NormalizedCompensation | None,
    bundles: tuple[RankingFacts, ...],
    config: ScoringConfigurationV2,
    at: datetime,
    tier: str | None,
) -> DimensionResult:
    dimension = 'compensation'
    floor, preferred = profile.minimum_base_salary, profile.preferred_base_salary
    if not floor and not preferred:
        return _empty(dimension, 'salary_preference_unrestricted', status='not_applicable')
    if (
        pay is None
        or pay.status != 'available'
        or pay.representative_value is None
        or pay.evidence_quality == 0
        or pay.normalized_currency != profile.salary_currency
        or pay.normalized_basis != profile.salary_basis
    ):
        return _empty(dimension, 'representative_salary_unavailable')
    assertions = [a for b in bundles for a in b.compensation if _quality(a.source, config, at) > 0]
    if tier is not None:
        assertions = [a for a in assertions if a.tier == tier]
    elif len({a.tier for a in assertions}) > 1:
        return _empty(dimension, 'salary_tier_selection_required')
    signatures = {
        (a.minimum, a.maximum, a.currency, a.period, a.basis, a.component) for a in assertions
    }
    if len(signatures) > 1:
        return _empty(dimension, 'conflicting_salary_evidence')
    if assertions and not any(
        (a.minimum, a.maximum, a.currency, a.period, a.basis, a.component)
        == (
            pay.original_minimum,
            pay.original_maximum,
            pay.original_currency,
            pay.original_period,
            pay.original_basis,
            'base',
        )
        for a in assertions
    ):
        return _empty(dimension, 'salary_normalization_mismatch')
    if any(b.compensation for b in bundles) and not assertions:
        return _empty(dimension, 'salary_evidence_unsupported')
    value = pay.representative_value
    try:
        if preferred and (floor is None or floor == 0):
            score = Decimal(100) if value >= preferred else 100 * value / preferred
        elif preferred and floor is not None and preferred > floor:
            score = (
                Decimal(100)
                if value >= preferred
                else (
                    50 * value / floor
                    if value < floor
                    else 50 + 50 * (value - floor) / (preferred - floor)
                )
            )
        else:
            assert floor is not None
            score = Decimal(100 if value >= floor else 0)
        return _known(
            dimension,
            score,
            pay.evidence_quality,
            'salary_utility',
            evidence=tuple(a.source.evidence_id for a in assertions),
            terms={'representative_salary': value},
        )
    except DecimalException:
        return _empty(dimension, 'salary_numeric_range')


def _remote(
    profile: Profile,
    bundles: tuple[RankingFacts, ...],
    config: ScoringConfigurationV2,
    at: datetime,
) -> DimensionResult:
    dimension = 'remote_compatibility'
    if not profile.preferred_work_modes:
        return _empty(dimension, 'work_mode_preference_unrestricted', status='not_applicable')
    groups = {}
    relevant = [b for b in bundles if b.routes or b.routes_complete]
    for bundle in relevant:
        for route in bundle.routes:
            quality = _quality(route.source, config, at)
            if quality > 0:
                groups.setdefault(route.route_id, []).append((route, quality))
    valid = [
        entry
        for values in groups.values()
        if len({r.mode for r, _ in values}) == 1
        for entry in values
    ]
    matches = [
        (route, quality) for route, quality in valid if route.mode in profile.preferred_work_modes
    ]
    if matches:
        route, quality = min(matches, key=lambda pair: (-pair[1], content_hash(pair[0])))
        return _known(
            dimension,
            Decimal(100),
            quality,
            'preferred_work_mode_offered',
            evidence=(route.source.evidence_id,),
        )
    complete = relevant and all(b.routes_complete for b in relevant)
    total = sum(len(b.routes) for b in relevant)
    if complete and valid and len(valid) == total:
        return _known(
            dimension,
            Decimal(0),
            min(q for _, q in valid),
            'preferred_work_modes_not_offered',
            evidence=tuple(r.source.evidence_id for r, _ in valid),
        )
    return _empty(dimension, 'work_modes_incomplete_or_conflicting')


def _technical(
    profile: Profile,
    bundles: tuple[RankingFacts, ...],
    config: ScoringConfigurationV2,
    at: datetime,
) -> DimensionResult:
    dimension = 'technical_fit'
    if not profile.core_technologies:
        return _empty(dimension, 'candidate_skills_unknown')

    def tokens(values):
        return frozenset(config.technology_aliases.get(v.casefold(), v.casefold()) for v in values)

    options = [
        (a, tokens(a.technologies), _quality(a.source, config, at))
        for b in bundles
        for a in b.technologies
    ]
    usable = [(a, values, q) for a, values, q in options if q > 0]
    complete = [(a, values, q) for a, values, q in usable if a.complete]
    if not complete:
        return _empty(dimension, 'technical_requirements_incomplete')
    required = complete[0][1]
    if any(values != required for _, values, _ in complete) or any(
        not values <= required for _, values, _ in usable
    ):
        return _empty(dimension, 'conflicting_technical_requirements')
    if not required:
        return _empty(dimension, 'no_required_technologies', status='not_applicable')
    chosen, _, quality = min(complete, key=lambda option: (-option[2], content_hash(option[0])))
    matched = len(tokens(profile.core_technologies) & required)
    return _known(
        dimension,
        Decimal(100) * matched / len(required),
        quality,
        'required_skill_overlap',
        evidence=(chosen.source.evidence_id,),
        terms={'matched_required': Decimal(matched), 'total_required': Decimal(len(required))},
    )


def _review(
    dimension: str, bundles: tuple[RankingFacts, ...], config: ScoringConfigurationV2, at: datetime
) -> DimensionResult:
    candidates = []
    provider_ids: dict[str, set[str]] = {}
    for bundle in bundles:
        for rating in bundle.ratings:
            if (
                config.category_dimensions.get(rating.category) != dimension
                or rating.source.source not in config.source_priority
            ):
                continue
            quality = _quality(rating.source, config, at)
            age = at - rating.source.observed_at
            if (age.days, age.seconds, age.microseconds) > (config.review_max_age_days, 0, 0):
                continue
            factor = (
                config.missing_review_count_factor
                if rating.review_count is None
                else min(Decimal(rating.review_count) / config.review_count_saturation, Decimal(1))
            )
            quality = min(quality, config.evidence_weights['structured']) * factor
            if quality == 0:
                continue
            provider_ids.setdefault(rating.source.source, set()).add(rating.company_id)
            candidates.append((rating, quality))
    if any(len(ids) > 1 for ids in provider_ids.values()):
        return _empty(dimension, 'company_association_ambiguous')
    if not candidates:
        return _empty(dimension, 'category_rating_unavailable')
    rating, quality = min(
        candidates,
        key=lambda pair: (
            config.source_priority.index(pair[0].source.source),
            at - pair[0].source.observed_at,
            content_hash(pair[0]),
        ),
    )
    return _known(
        dimension,
        25 * (rating.value - 1),
        quality,
        'employee_reported_security_proxy'
        if dimension == 'stability'
        else 'captured_category_rating',
        evidence=(rating.source.evidence_id,),
        terms={'rating': rating.value},
    )


def score_dimensions(
    profile: Profile,
    bundles: tuple[RankingFacts, ...],
    *,
    compensation: NormalizedCompensation | None,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    selected_tier: str | None = None,
) -> tuple[DimensionResult, ...]:
    profile = validated_profile(profile)
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    bundles = tuple(RankingFacts.model_validate_json(canonical_json(b)) for b in bundles)
    at = utc(evaluated_at)
    if compensation is not None:
        compensation = NormalizedCompensation.model_validate_json(canonical_json(compensation))
        if (
            compensation.status == 'available'
            and compensation.scoring_config_id != config.scoring_config_id
        ):
            raise ValueError('normalization references a different scoring configuration')
    values = []
    with localcontext(arithmetic_context()):
        for dimension in DIMENSIONS:
            if profile.weights[dimension] == 0:
                value = _empty(dimension, 'zero_weight', status='disabled')
            elif dimension == 'compensation':
                value = _compensation(profile, compensation, bundles, config, at, selected_tier)
            elif dimension == 'remote_compatibility':
                value = _remote(profile, bundles, config, at)
            elif dimension == 'technical_fit':
                value = _technical(profile, bundles, config, at)
            else:
                value = _review(dimension, bundles, config, at)
            values.append(value)
    return tuple(values)
