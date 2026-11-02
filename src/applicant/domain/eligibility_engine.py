"""Evaluate all job-wide constraints; keep route diagnostics outside v2 history models."""

from datetime import datetime

from .canonical import canonical_json
from .compensation import NormalizedCompensation
from .eligibility import (
    Profile,
    compensation_eligibility,
    employment_eligibility,
    validated_profile,
)
from .ranking_facts import RankingFacts
from .ranking_v2 import EligibilityDecision, EngineContract, ScoringConfigurationV2
from .work_routes import RouteCheck, evaluate_routes


class EligibilityEvaluation(EngineContract):
    decision: EligibilityDecision
    route_checks: tuple[RouteCheck, ...]
    notes: tuple[str, ...] = ()


def evaluate_eligibility(
    profile: Profile,
    bundles: tuple[RankingFacts, ...],
    *,
    compensation: NormalizedCompensation | None,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    selected_tier: str | None = None,
) -> EligibilityEvaluation:
    profile = validated_profile(profile)
    bundles = tuple(RankingFacts.model_validate_json(canonical_json(b)) for b in bundles)
    salary = compensation_eligibility(
        profile,
        compensation,
        tuple(a for b in bundles for a in b.compensation),
        config=config,
        evaluated_at=evaluated_at,
        selected_tier=selected_tier,
    )
    employment = employment_eligibility(
        profile,
        tuple(o for b in bundles for o in b.employment),
        config=config,
        evaluated_at=evaluated_at,
    )
    routes = evaluate_routes(profile, bundles, config=config, evaluated_at=evaluated_at)
    notes = (
        ('unstructured_preference_not_evaluated',)
        if profile.preferred_regions or profile.timezone_overlap_tolerance
        else ()
    )
    return EligibilityEvaluation(
        decision=EligibilityDecision(criteria=(salary, employment, routes.criterion)),
        route_checks=routes.checks,
        notes=notes,
    )
