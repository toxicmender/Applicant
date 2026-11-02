"""Pure base-pay normalization using explicit facts and exact run inputs."""

from __future__ import annotations

from decimal import Decimal, DecimalException, Underflow, localcontext

from .canonical import canonical_json
from .compensation import NormalizedCompensation
from .contracts import Basis, CurrencyCode, currency
from .fx import FXSnapshot
from .ranking_facts import CompensationFacts
from .ranking_math import arithmetic_context
from .ranking_v2 import FXSnapshotV2, ScoringConfigurationV2


def normalize_compensation(
    facts: CompensationFacts,
    *,
    target_currency: CurrencyCode,
    target_basis: Basis,
    fx: FXSnapshot | FXSnapshotV2,
    config: ScoringConfigurationV2,
) -> NormalizedCompensation:
    """Normalize one selected tier. Selecting between tiers belongs to the caller.

    Available bounds remain useful when the requested representative is unknown.
    Invalid supplied contracts fail validation; missing facts produce diagnostics.
    FX acquisition/freshness selection happens before this pure calculation.
    """
    facts = CompensationFacts.model_validate_json(canonical_json(facts))
    fx = type(fx).model_validate_json(canonical_json(fx))
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    currency(target_currency)
    if target_basis not in {'gross', 'net'}:
        raise ValueError('unsupported target salary basis')
    original = {
        'original_text': facts.original_text,
        'original_minimum': facts.minimum,
        'original_maximum': facts.maximum,
        'original_currency': facts.currency,
        'original_period': facts.period,
        'original_basis': facts.basis,
    }

    def missing(*warnings: str, unavailable: bool = False) -> NormalizedCompensation:
        return NormalizedCompensation.model_validate(
            original
            | {
                'status': 'unavailable' if unavailable else 'review_required',
                'warnings': warnings,
            }
        )

    issues = []
    if facts.shape == 'unknown':
        issues.append('salary_bounds_unknown')
    if facts.currency is None:
        issues.append('salary_currency_unknown')
    if facts.period is None:
        issues.append('salary_period_unknown')
    if facts.basis is None:
        issues.append('salary_basis_unknown')
    elif facts.basis != target_basis:
        issues.append('salary_basis_mismatch')
    if facts.component != 'base':
        issues.append(
            'salary_base_component_unknown'
            if facts.component == 'unknown'
            else 'total_pay_not_base'
        )
    if config.evidence_weights[facts.source.quality] == 0:
        issues.append('salary_evidence_unsupported')
    if issues:
        return missing(*issues)
    assert facts.period is not None and facts.currency is not None
    source_currency = facts.currency
    same_currency = facts.currency == target_currency
    if not same_currency and (facts.currency not in fx.rates or target_currency not in fx.rates):
        return missing('fx_rate_unavailable', unavailable=True)
    if not same_currency and fx.provenance == 'static' and not config.allow_static_fx:
        return missing('static_fx_disabled')

    with localcontext(arithmetic_context()) as context:
        try:
            factors = {
                'annual': Decimal(1),
                'monthly': config.annual_months,
                'weekly': config.annual_weeks,
                'daily': config.annual_days,
                'hourly': config.annual_hours,
            }
            factor = factors[facts.period]  # validated above

            def convert(amount: Decimal | None) -> Decimal | None:
                if amount is None:
                    return None
                annual = amount * factor
                if same_currency:
                    return annual
                return annual * fx.rates[target_currency] / fx.rates[source_currency]

            low, high = convert(facts.minimum), convert(facts.maximum)
            strategy = config.representative_salary_strategy
            representative = (
                low
                if strategy == 'minimum'
                else high
                if strategy == 'maximum'
                else ((low + high) / 2 if low is not None and high is not None else None)
            )
            quantum = Decimal('.01')
            values = [
                None if value is None else value.quantize(quantum)
                for value in (low, high, representative)
            ]
            if context.flags[Underflow]:
                return missing('normalization_numeric_range')
        except DecimalException:
            return missing('normalization_numeric_range')
    fx_quality = (
        Decimal(1)
        if same_currency
        else (config.static_fx_quality if fx.provenance == 'static' else config.market_fx_quality)
    )
    warnings = () if representative is not None else ('representative_bound_missing',)
    return NormalizedCompensation.model_validate(
        original
        | {
            'status': 'available',
            'annual_minimum': values[0],
            'annual_maximum': values[1],
            'representative_value': values[2],
            'normalized_currency': target_currency,
            'normalized_basis': target_basis,
            'normalization_method': strategy,
            'normalizer_version': config.normalizer_version,
            'rounding_policy': config.rounding_policy,
            'assumed_annual_hours': config.annual_hours if facts.period == 'hourly' else None,
            'assumed_annual_days': config.annual_days if facts.period == 'daily' else None,
            'fx_snapshot_id': fx.snapshot_id,
            'scoring_config_id': config.scoring_config_id,
            'evidence_quality': min(config.evidence_weights[facts.source.quality], fx_quality),
            'warnings': warnings,
        }
    )
