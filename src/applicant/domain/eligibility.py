"""Pure eligibility predicates. Unknown evidence is never an exclusion."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, DecimalException, localcontext
from heapq import nsmallest
from typing import Literal

from .canonical import canonical_json, utc
from .compensation import NormalizedCompensation
from .preferences import CandidateProfile
from .ranking_facts import CompensationFacts, EmploymentOptions, FactSource
from .ranking_math import arithmetic_context
from .ranking_v2 import (
    CandidateProfileV2,
    ConflictAssertion,
    CriterionResult,
    EvidenceConflict,
    ScoringConfigurationV2,
)

Profile = CandidateProfile | CandidateProfileV2
Status = Literal['pass', 'fail', 'unknown', 'not_applicable']


def validated_profile(profile: Profile) -> Profile:
    with localcontext(arithmetic_context()):
        return type(profile).model_validate_json(canonical_json(profile))


def supported(source: FactSource, config: ScoringConfigurationV2, evaluated_at: datetime) -> bool:
    if source.observed_at > evaluated_at:
        raise ValueError('evidence observation is after evaluation time')
    return (
        source.quality in {'explicit', 'verified', 'structured'}
        and config.evidence_weights[source.quality] > 0
    )


def result(
    criterion: str,
    status: Status,
    reason: str,
    explanation: str,
    sources: tuple[FactSource, ...] = (),
) -> CriterionResult:
    return CriterionResult(
        criterion=criterion,
        status=status,
        reason_code=reason,
        explanation=explanation,
        evidence_ids=tuple(dict.fromkeys(s.evidence_id for s in sources)),
    )


def conflict(
    criterion: str,
    field: str,
    assertions: tuple[tuple[FactSource, str], ...],
    *,
    route_id: str | None = None,
) -> EvidenceConflict:
    # One representative per distinct value keeps explanations bounded, without
    # enumerating every conflicting pair. Sort the bounded witnesses for replay.
    witnesses: dict[str, FactSource] = {}
    for source, value in assertions:
        previous = witnesses.get(value)
        if previous is None or source.evidence_id < previous.evidence_id:
            witnesses[value] = source
    details = tuple(
        ConflictAssertion(
            evidence_id=source.evidence_id,
            source=source.source,
            observed_at=source.observed_at,
            normalized_value=value,
            excerpt=source.excerpt,
        )
        for value, source in nsmallest(2, witnesses.items())
    )
    explanation = (
        f'{field} needs review: '
        + '; '.join(
            f'{item.source} states {item.normalized_value} ({item.excerpt})' for item in details
        )
        + '.'
    )
    return EvidenceConflict(
        criterion=criterion,
        route_id=route_id,
        assertions=details,
        explanation=explanation,
        needs_verification=f'Confirm {field.lower()} with the employer.',
    )


def salary_floor(profile: Profile, pay: NormalizedCompensation | None) -> CriterionResult:
    profile = validated_profile(profile)
    name = 'compensation_floor'
    if profile.minimum_base_salary is None:
        return result(
            name,
            'not_applicable',
            'salary_floor_unrestricted',
            'No minimum base salary was requested.',
        )
    if pay is None:
        return result(
            name, 'unknown', 'salary_unavailable', 'Comparable annual base pay is missing.'
        )
    pay = NormalizedCompensation.model_validate_json(canonical_json(pay))
    if (
        pay.status != 'available'
        or pay.evidence_quality == 0
        or pay.normalized_currency != profile.salary_currency
        or pay.normalized_basis != profile.salary_basis
    ):
        return result(
            name,
            'unknown',
            'salary_not_comparable',
            'Pay is unavailable, unsupported, or in different currency/basis.',
        )
    with localcontext(arithmetic_context()):
        try:
            floor = profile.minimum_base_salary.quantize(Decimal('.01'))
            low = (
                None if pay.annual_minimum is None else pay.annual_minimum.quantize(Decimal('.01'))
            )
            high = (
                None if pay.annual_maximum is None else pay.annual_maximum.quantize(Decimal('.01'))
            )
        except DecimalException:
            return result(
                name,
                'unknown',
                'salary_numeric_range',
                'Salary cannot be compared under the recorded rounding policy.',
            )
    if low is not None and low >= floor:
        return result(
            name, 'pass', 'salary_floor_met', f'Annual minimum {low} meets the floor {floor}.'
        )
    if high is not None and high < floor:
        return result(
            name, 'fail', 'salary_below_floor', f'Annual maximum {high} is below the floor {floor}.'
        )
    return result(
        name,
        'unknown',
        'salary_floor_not_guaranteed',
        'The available bounds do not guarantee the salary floor.',
    )


def compensation_eligibility(
    profile: Profile,
    pay: NormalizedCompensation | None,
    assertions: tuple[CompensationFacts, ...],
    *,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    selected_tier: str | None = None,
) -> CriterionResult:
    baseline = salary_floor(profile, pay)
    if baseline.status == 'not_applicable' or not assertions:
        return baseline
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    evaluated_at = utc(evaluated_at)
    usable = []
    for assertion in assertions:
        assertion = CompensationFacts.model_validate_json(canonical_json(assertion))
        if supported(assertion.source, config, evaluated_at):
            usable.append(assertion)
    if selected_tier is not None:
        usable = [a for a in usable if a.tier == selected_tier]
    elif len({a.tier for a in usable}) > 1:
        return result(
            'compensation_floor',
            'unknown',
            'salary_tier_selection_required',
            'Select the applicable pay tier before evaluating its salary floor.',
        )
    if not usable:
        return result(
            'compensation_floor',
            'unknown',
            'salary_evidence_unsupported',
            'No supported salary assertion exists for the selected tier.',
        )
    signatures = tuple(
        (
            a.source,
            canonical_json(
                a.model_dump(
                    include={
                        'minimum',
                        'maximum',
                        'currency',
                        'period',
                        'basis',
                        'component',
                    }
                )
            ),
        )
        for a in usable
        if a.shape != 'unknown' and a.currency and a.period and a.basis and a.component == 'base'
    )
    if len({value for _, value in signatures}) > 1:
        item = conflict('compensation_floor', 'Base salary', signatures)
        return CriterionResult(
            criterion='compensation_floor',
            status='unknown',
            reason_code=item.code,
            explanation=item.explanation,
            conflicts=(item,),
        )
    if not signatures:
        return result(
            'compensation_floor',
            'unknown',
            'salary_evidence_incomplete',
            'Source salary units, basis or bounds are incomplete.',
        )
    if pay is not None and not any(
        (a.minimum, a.maximum, a.currency, a.period, a.basis)
        == (
            pay.original_minimum,
            pay.original_maximum,
            pay.original_currency,
            pay.original_period,
            pay.original_basis,
        )
        for a in usable
    ):
        return result(
            'compensation_floor',
            'unknown',
            'salary_normalization_mismatch',
            'Normalized pay does not reference the selected source salary facts.',
        )
    return baseline


def employment_eligibility(
    profile: Profile,
    options: tuple[EmploymentOptions, ...],
    *,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
) -> CriterionResult:
    profile = validated_profile(profile)
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    evaluated_at = utc(evaluated_at)
    name = 'employment'
    if profile.employment_preference == 'either':
        return result(
            name,
            'not_applicable',
            'employment_unrestricted',
            'Either employment arrangement is acceptable.',
        )
    usable = []
    for option in options:
        option = EmploymentOptions.model_validate_json(canonical_json(option))
        if supported(option.source, config, evaluated_at):
            usable.append(option)
    if not usable:
        return result(
            name,
            'unknown',
            'employment_unknown',
            'Permanent/contract arrangements are not supported by explicit evidence.',
        )
    complete = [frozenset(option.arrangements) for option in usable if option.complete]
    all_arrangements = {arrangement for option in usable for arrangement in option.arrangements}
    if complete and (
        any(value != complete[0] for value in complete) or not all_arrangements <= complete[0]
    ):
        item = conflict(
            name,
            'Employment arrangements',
            tuple(
                (
                    option.source,
                    canonical_json(
                        {
                            'arrangements': sorted(set(option.arrangements)),
                        }
                    ),
                )
                for option in usable
            ),
        )
        return CriterionResult(
            criterion=name,
            status='unknown',
            reason_code=item.code,
            explanation=item.explanation,
            conflicts=(item,),
            evidence_ids=tuple(sorted({option.source.evidence_id for option in usable})),
        )
    accepted = 'permanent' if profile.employment_preference == 'permanent_only' else 'contract'
    sources = tuple(option.source for option in usable)
    if accepted in all_arrangements:
        return result(
            name,
            'pass',
            'employment_match',
            f'An explicit {accepted} option is available.',
            sources,
        )
    if complete:
        return result(
            name,
            'fail',
            'employment_mismatch',
            f'Complete arrangement evidence offers no {accepted} option.',
            sources,
        )
    return result(
        name,
        'unknown',
        'employment_incomplete',
        'Incomplete arrangement evidence cannot rule out an acceptable option.',
        sources,
    )
