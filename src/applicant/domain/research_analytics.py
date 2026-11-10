"""Pure point-in-time metrics over explicitly selected immutable versions."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from statistics import median

from .fx import FXSnapshot
from .research_identity import match_jobs
from .research_time import zone

ALIASES = {
    'py': 'python',
    'python3': 'python',
    'python 3': 'python',
    'postgres': 'postgresql',
    'js': 'javascript',
    'nodejs': 'node.js',
    'node': 'node.js',
    'tf': 'tensorflow',
    'pytorch': 'pytorch',
}


def technology_name(value: str) -> str:
    value = value.casefold().strip()
    return ALIASES.get(value, value)


def analytics(
    data,
    *,
    as_of,
    applicant_timezone,
    created_at,
    group_by='technology',
    window_days=30,
    currency=None,
    fx=None,
    origin=None,
    role=None,
    geography=None,
):
    if group_by not in ('technology', 'stack') or window_days < 1 or window_days > 3660:
        raise ValueError('invalid analytics grouping/window')
    day = date.fromisoformat(as_of)
    tz = zone(applicant_timezone)
    if day > created_at.astimezone(tz).date():
        raise ValueError('as-of date cannot be in the future')
    cutoff = min(
        datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).astimezone(timezone.utc),
        created_at,
    )
    start = cutoff - timedelta(days=window_days)
    observations = [
        x
        for x in data['observations']
        if x.acquired_at < cutoff and (x.observed_at is None or x.observed_at < cutoff)
    ]
    groups = match_jobs(observations)
    by_id = {x.job_version_id: x for x in observations}
    evidence = [
        e
        for e in data['evidence']
        if e.acquired_at < cutoff and (e.observed_at is None or e.observed_at < cutoff)
    ]
    rates = FXSnapshot.model_validate(fx) if fx else None
    if rates and rates.effective_date > day:
        raise ValueError('FX effective date follows the analysis date')
    members, metrics = (
        [],
        defaultdict(
            lambda: {
                'openings': 0,
                'origins': {'direct': 0, 'agency': 0, 'unknown': 0},
                'pay': defaultdict(list),
                'alternatives': 0,
            }
        ),
    )
    for group in groups:
        selected = [by_id[key] for key in group['job_version_ids']]
        if group['employer_conflict'] or any(x.status == 'closed' for x in selected):
            continue
        active = [x for x in selected if x.status == 'open' and start <= x.acquired_at < cutoff]
        if not active:
            continue
        representative = sorted(
            active, key=lambda x: (x.origin != 'direct', x.origin != 'agency', x.job_version_id)
        )[0]
        party = representative.origin
        if (
            (origin and party != origin)
            or (role and role.casefold() not in representative.job.title.casefold())
            or (
                geography
                and geography.casefold() not in (representative.job.location or '').casefold()
            )
        ):
            continue
        ids = set(group['job_version_ids'])
        facts = [e for e in evidence if e.job_version_id in ids]
        parties = [
            p
            for p in data['parties']
            if p.job_version_id in ids and p.recorded_at is not None and p.recorded_at < cutoff
        ]
        technologies = sorted(
            {
                technology_name(e.payload.technology)
                for e in facts
                if e.payload.kind == 'technology'
                and e.payload.requirement == 'required'
                and not e.payload.negated
                and not e.payload.alternative_group
            }
        )
        alternatives = sorted(
            {
                str(e.payload.alternative_group)
                for e in facts
                if e.payload.kind == 'technology'
                and e.payload.alternative_group
                and not e.payload.negated
            }
        )
        compensation = [
            e
            for e in facts
            if e.payload.kind == 'compensation'
            and e.payload.measurement == 'posting'
            and e.payload.component == 'base'
            and e.payload.basis == 'gross'
            and e.payload.period in ('annual', 'monthly', 'weekly')
            and e.payload.minimum is not None
            and e.payload.maximum is not None
        ]
        candidates = {}
        for e in compensation:
            p = e.payload
            key = (p.minimum, p.maximum, p.currency, p.period)
            candidates[key] = e
        salary, pay_currency, reason, salary_id = None, None, 'missing_comparable_salary', None
        if len(candidates) == 1:
            chosen = next(iter(candidates.values()))
            p = chosen.payload
            salary = (
                (p.minimum + p.maximum) / 2 * {'annual': 1, 'monthly': 12, 'weekly': 52}[p.period]
            )
            pay_currency, salary_id, reason = p.currency, chosen.evidence_id, 'range_midpoint'
            if currency and currency != pay_currency:
                if rates and pay_currency in rates.rates and currency in rates.rates:
                    salary = (salary * rates.rates[currency] / rates.rates[pay_currency]).quantize(
                        Decimal('.01')
                    )
                    pay_currency = currency
                else:
                    salary, reason = None, 'missing_fx'
        elif len(candidates) > 1:
            reason = 'conflicting_salary_observations'
        for observation in selected:
            members.append(
                {
                    'job_version_id': observation.job_version_id,
                    'job_group_id': group['job_group_id'],
                    'match_revision': group['match_revision'],
                    'demand_representative': observation == representative,
                    'origin': party,
                    'evidence_ids': sorted(e.evidence_id for e in facts),
                    'party_ids': sorted(p.party_id for p in parties),
                    'compensation_evidence_id': salary_id,
                    'compensation_reason': reason,
                    'normalized_salary': str(salary) if salary is not None else None,
                    'currency': pay_currency,
                }
            )
        labels = (
            technologies
            if group_by == 'technology'
            else [' + '.join(technologies)]
            if technologies
            else []
        )
        for label in labels:
            metric = metrics[label]
            metric['openings'] += 1
            metric['origins'][party] += 1
            metric['alternatives'] += bool(alternatives)
            if salary is not None:
                metric['pay'][pay_currency].append(salary)
    rows = []
    for label, metric in sorted(metrics.items(), key=lambda item: (-item[1]['openings'], item[0])):
        pay = [
            {
                'currency': c,
                'sample_size': len(values),
                'coverage': len(values) / metric['openings'],
                'median_annual_gross_base': str(median(values)) if len(values) >= 5 else None,
                'status': 'available' if len(values) >= 5 else 'insufficient_sample',
            }
            for c, values in sorted(metric['pay'].items())
        ]
        rows.append(
            {
                'technology' if group_by == 'technology' else 'stack': label,
                'observed_openings': metric['openings'],
                'origins': metric['origins'],
                'jobs_with_alternatives': metric['alternatives'],
                'compensation': pay,
            }
        )
    return {
        'kind': 'technology_analytics',
        'schema_version': 1,
        'created_at': created_at.isoformat(),
        'as_of': as_of,
        'cutoff': cutoff.isoformat(),
        'knowledge_cutoff': cutoff.isoformat(),
        'window_start_inclusive': start.isoformat(),
        'window_end_exclusive': cutoff.isoformat(),
        'timezone': applicant_timezone,
        'policy_version': 'research-analytics-1',
        'taxonomy_version': 'tech-1',
        'group_by': group_by,
        'filters': {'origin': origin, 'role': role, 'geography': geography},
        'fx': rates.model_dump(mode='json') if rates else None,
        'rounding': 'Decimal half-even to 0.01 after conversion',
        'members': members,
        'job_version_ids': [x['job_version_id'] for x in members],
        'groups': groups,
        'evidence': [
            e.model_dump(mode='json')
            for e in evidence
            if any(e.evidence_id in m['evidence_ids'] for m in members)
        ],
        'rows': rows,
        'coverage': {
            'providers': sorted({x.job.source for x in observations}),
            'observations_before_cutoff': len(observations),
            'participating_versions': len(members),
            'note': 'Observed sample only; unknown and stale jobs are excluded. Salary associations are not causal.',
        },
    }
