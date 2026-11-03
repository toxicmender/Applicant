"""Decimal annualization and nominal FX; wage definitions remain independent."""

from decimal import Decimal, DecimalException, Underflow, localcontext

from .benchmarks import BenchmarkObservation
from .canonical import canonical_json, content_hash
from .contracts import CurrencyCode, Positive
from .fx import FXSnapshot
from .ranking_math import arithmetic_context
from .ranking_v2 import FXSnapshotV2


def normalize_observation(
    observation: BenchmarkObservation,
    *,
    target_currency: CurrencyCode,
    fx: FXSnapshot | FXSnapshotV2,
    annual_hours: Positive | None = None,
    annual_days: Positive | None = None,
) -> dict:
    observation = BenchmarkObservation.model_validate_json(canonical_json(observation))
    fx = type(fx).model_validate_json(canonical_json(fx))
    # Validate caller units without relying on arithmetic to reject bool/NaN.
    from pydantic import TypeAdapter

    target_currency = TypeAdapter(CurrencyCode).validate_python(target_currency)
    hours = (
        TypeAdapter(Positive).validate_python(annual_hours) if annual_hours is not None else None
    )
    days = TypeAdapter(Positive).validate_python(annual_days) if annual_days is not None else None
    reasons = []
    factor = {'annual': Decimal(1), 'monthly': Decimal(12), 'hourly': hours, 'daily': days}[
        observation.period
    ]
    if observation.status != 'available':
        reasons.append(observation.status)
    if factor is None:
        reasons.append('working_time_assumption_required')
    if (
        observation.currency != target_currency
        and not {observation.currency, target_currency} <= fx.rates.keys()
    ):
        reasons.append('fx_rate_unavailable')
    value = None
    try:
        with localcontext(arithmetic_context()) as context:
            if (
                observation.amount is not None
                and factor is not None
                and 'fx_rate_unavailable' not in reasons
            ):
                rate = (
                    Decimal(1)
                    if observation.currency == target_currency
                    else fx.rates[target_currency] / fx.rates[observation.currency]
                )
                value = (observation.amount * factor * rate).quantize(Decimal('.01'))
                if context.flags[Underflow]:
                    raise ValueError('benchmark arithmetic is outside supported range')
    except DecimalException as error:
        raise ValueError('benchmark arithmetic is outside supported range') from error
    return {
        'policy': 'benchmark-normalization-v1',
        'observation_id': observation.observation_id,
        'original_amount': observation.amount,
        'original_currency': observation.currency,
        'original_period': observation.period,
        'annual_amount': value,
        'target_currency': target_currency,
        'annual_factor': factor,
        'annualized_equivalent': observation.period != 'annual',
        'fx_snapshot_id': fx.snapshot_id,
        'fx_snapshot_hash': content_hash(fx),
        'status': 'available' if not reasons else 'context_only',
        'reasons': reasons,
    }


def cohort_groups(snapshots) -> list[dict]:
    groups = {}
    for snapshot in snapshots:
        for observation in snapshot.observations:
            # Currency/period remain separate until an explicit normalization.
            key = content_hash(
                {
                    'cohort': observation.cohort,
                    'currency': observation.currency,
                    'period': observation.period,
                }
            )
            group = groups.setdefault(
                key,
                {
                    'cohort_id': key,
                    'cohort': observation.cohort.model_dump(),
                    'currency': observation.currency,
                    'period': observation.period,
                    'observations': [],
                },
            )
            group['observations'].append(
                {'snapshot_id': snapshot.snapshot_id, 'observation_id': observation.observation_id}
            )
    return [groups[k] for k in sorted(groups)]
