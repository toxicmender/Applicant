"""Classify route assertions once, then short-circuit alternative routes safely."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from .canonical import canonical_json, utc
from .eligibility import Profile, conflict, result, supported, validated_profile
from .ranking_facts import RankingFacts, WorkRoute
from .ranking_v2 import (
    CandidateProfileV2,
    CriterionResult,
    EngineContract,
    EvidenceConflict,
    ScoringConfigurationV2,
)


class RouteCheck(EngineContract):
    route_id: str
    check: Literal['required_work_mode', 'remote_location', 'route']
    status: Literal['pass', 'fail', 'unknown', 'not_applicable', 'not_evaluated']
    reason_code: str
    conflicts: tuple[EvidenceConflict, ...] = ()


class RouteEvaluation(EngineContract):
    criterion: CriterionResult
    checks: tuple[RouteCheck, ...]


def _location(
    profile: Profile, route: WorkRoute
) -> tuple[Literal['pass', 'fail', 'unknown', 'not_applicable'], str]:
    if route.mode == 'Onsite':
        return 'not_applicable', 'onsite_route'
    if route.remote_scope == 'worldwide':
        return 'pass', 'remote_worldwide'
    if route.remote_scope == 'unknown':
        return 'unknown', 'remote_scope_unknown'
    country = profile.work_country if isinstance(profile, CandidateProfileV2) else None
    if country is None:
        return 'unknown', 'work_country_unknown'
    return (
        ('pass', 'remote_country_match')
        if country in frozenset(route.allowed_countries)
        else ('fail', 'remote_country_excluded')
    )


def evaluate_routes(
    profile: Profile,
    bundles: tuple[RankingFacts, ...],
    *,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
) -> RouteEvaluation:
    profile = validated_profile(profile)
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    evaluated_at = utc(evaluated_at)
    groups: dict[str, list[WorkRoute]] = {}
    relevant = []
    for bundle in bundles:
        bundle = RankingFacts.model_validate_json(canonical_json(bundle))
        if bundle.routes or bundle.routes_complete:
            relevant.append(bundle.routes_complete)
        for route in bundle.routes:
            groups.setdefault(route.route_id, []).append(route)
    complete = bool(relevant) and all(relevant)
    # Preclassify every assertion before any alternative passes. The same route
    # ID across evidence bundles means repeated assertions, not alternative modes.
    prepared = []
    for route_id, assertions in groups.items():
        usable = [r for r in assertions if supported(r.source, config, evaluated_at)]
        conflicts = []
        for field in ('mode', 'scope'):
            values = []
            for route in usable:
                if field == 'mode':
                    value = route.mode
                elif route.remote_scope == 'unknown':
                    continue
                else:
                    value = canonical_json(
                        {'scope': route.remote_scope, 'countries': route.allowed_countries}
                    )
                values.append((route.source, value))
            if len({value for _, value in values}) > 1:
                conflicts.append(
                    conflict('work_route', f'Work route {field}', tuple(values), route_id=route_id)
                )
        # Prefer an explicit scope to an unknown assertion. Tie-breaking depends
        # on captured data, not evidence iteration order.
        route = (
            min(usable, key=lambda r: (r.remote_scope == 'unknown', canonical_json(r)))
            if usable
            else None
        )
        residual = any(r.residual_restrictions for r in usable)
        prepared.append((route_id, route, residual, tuple(conflicts)))
    checks = []
    unknown = False
    found = False
    relevant_conflicts = []
    sources = []
    required = (
        frozenset(profile.required_work_modes)
        if isinstance(profile, CandidateProfileV2)
        else frozenset()
    )
    for route_id, route, residual, conflicts in prepared:
        if found:
            checks.append(
                RouteCheck(
                    route_id=route_id,
                    check='route',
                    status='not_evaluated',
                    reason_code='alternative_already_verified',
                    conflicts=conflicts,
                )
            )
            continue
        if conflicts or route is None:
            unknown = True
            relevant_conflicts.extend(conflicts)
            checks.append(
                RouteCheck(
                    route_id=route_id,
                    check='route',
                    status='unknown',
                    reason_code='conflicting_evidence'
                    if conflicts
                    else 'route_evidence_unsupported',
                    conflicts=conflicts,
                )
            )
            continue
        mode_ok = not required or route.mode in required
        sources.append(route.source)
        checks.append(
            RouteCheck(
                route_id=route_id,
                check='required_work_mode',
                status='pass' if mode_ok and required else 'not_applicable' if mode_ok else 'fail',
                reason_code='work_mode_match' if mode_ok else 'work_mode_mismatch',
            )
        )
        if not mode_ok:
            checks.append(
                RouteCheck(
                    route_id=route_id,
                    check='remote_location',
                    status='not_evaluated',
                    reason_code='work_mode_mismatch',
                )
            )
            continue
        status, reason = _location(profile, route)
        # Additional unknown restrictions cannot erase a known country failure.
        if residual and status == 'pass':
            status, reason = 'unknown', 'remote_residual_restrictions'
        checks.append(
            RouteCheck(
                route_id=route_id, check='remote_location', status=status, reason_code=reason
            )
        )
        if status in {'pass', 'not_applicable'}:
            found = True
        elif status == 'unknown':
            unknown = True
    if found:
        criterion = result(
            'work_route',
            'pass',
            'work_route_verified',
            'At least one offered route meets all evaluated mode/location constraints.',
            tuple(sources),
        )
    elif relevant_conflicts:
        criterion = CriterionResult(
            criterion='work_route',
            status='unknown',
            reason_code='conflicting_evidence',
            explanation=' '.join(c.explanation for c in relevant_conflicts),
            conflicts=tuple(relevant_conflicts),
        )
    elif unknown or not complete or not prepared:
        criterion = result(
            'work_route',
            'unknown',
            'work_routes_unresolved',
            'Missing, incomplete or unsupported route evidence needs review.',
            tuple(sources),
        )
    else:
        criterion = result(
            'work_route',
            'fail',
            'work_routes_excluded',
            'Every explicitly offered route fails a required mode or permitted-country condition.',
            tuple(sources),
        )
    return RouteEvaluation(criterion=criterion, checks=tuple(checks))
