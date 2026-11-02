"""Pure ranking-1 evaluation of exact immutable inputs in membership order."""

from datetime import datetime

from .canonical import canonical_json, content_hash, utc
from .compensation import NormalizedCompensation
from .eligibility import Profile, validated_profile
from .eligibility_engine import evaluate_eligibility
from .fx import FXSnapshot
from .normalization import normalize_compensation
from .ranking import JobVersion
from .ranking_facts import CompensationFacts, RankingFacts
from .ranking_salary import SalaryParse
from .ranking_v2 import (
    CriterionResult,
    EligibilityDecision,
    FXSnapshotV2,
    JobRankingV2,
    ScoringConfigurationV2,
)
from .scoring import score_job


def captured_facts(version: JobVersion) -> tuple[tuple[RankingFacts, ...], tuple[str, ...]]:
    bundles = []
    notes = []
    for evidence in version.evidence:
        payload = evidence.payload
        if payload.get('kind') == 'ranking_facts':
            if payload.get('schema_version', 1) != 1:
                notes.append('unsupported_evidence_schema')
                continue
            bundles.append(RankingFacts.model_validate(payload))
        elif payload.get('kind') == 'ranking_salary_parse':
            if payload.get('schema_version', 1) != 1:
                notes.append('unsupported_evidence_schema')
                continue
            parsed = SalaryParse.model_validate({k: v for k, v in payload.items() if k != 'kind'})
            notes.extend(parsed.warnings)
    return tuple(bundles), tuple(dict.fromkeys(notes))


def _salary(
    bundles: tuple[RankingFacts, ...],
    config: ScoringConfigurationV2,
    selected_tier: str | None,
    at: datetime,
) -> tuple[CompensationFacts | None, tuple[str, ...]]:
    candidates = []
    for bundle in bundles:
        for facts in bundle.compensation:
            if facts.source.observed_at > at:
                raise ValueError('salary observation is after evaluation time')
            if config.evidence_weights[facts.source.quality] > 0:
                candidates.append(facts)
    if selected_tier is not None:
        candidates = [f for f in candidates if f.tier == selected_tier]
        if not candidates:
            return None, ('salary_tier_unavailable',)
    elif len({f.tier for f in candidates}) > 1:
        return None, ('salary_tier_selection_required',)
    signatures = {
        canonical_json(
            f.model_dump(include={'minimum', 'maximum', 'currency', 'period', 'basis', 'component'})
        )
        for f in candidates
    }
    if len(signatures) > 1:
        return None, ('conflicting_salary_evidence',)
    if not candidates:
        return None, ()
    chosen = min(
        candidates,
        key=lambda f: (config.evidence_weights[f.source.quality].copy_negate(), content_hash(f)),
    )
    return chosen, ()


def evaluate_jobs(
    profile: Profile,
    jobs: tuple[JobVersion, ...],
    *,
    fx: FXSnapshot | FXSnapshotV2,
    config: ScoringConfigurationV2,
    evaluated_at: datetime,
    selected_tiers: dict[str, str] | None = None,
) -> tuple[JobRankingV2, ...]:
    profile = validated_profile(profile)
    config = ScoringConfigurationV2.model_validate_json(canonical_json(config))
    fx = type(fx).model_validate_json(canonical_json(fx))
    at = utc(evaluated_at)
    jobs = tuple(JobVersion.model_validate_json(canonical_json(j)) for j in jobs)
    if len({j.job_id for j in jobs}) != len(jobs):
        raise ValueError('select exactly one version of each job')
    tiers = {} if selected_tiers is None else dict(selected_tiers)
    if not tiers.keys() <= {j.job_id for j in jobs}:
        raise ValueError('salary tier selection is not an input job')
    results = []
    for job in jobs:
        if job.captured_at > at:
            raise ValueError('job capture is after evaluation time')
        bundles, notes = captured_facts(job)
        for evidence in job.evidence:
            if (
                evidence.payload.get('kind') in {'ranking_facts', 'ranking_salary_parse'}
                and evidence.observed_at > at
            ):
                raise ValueError('evidence observation is after evaluation time')
        salary, salary_notes = _salary(bundles, config, tiers.get(job.job_id), at)
        pay: NormalizedCompensation | None = None
        if salary is not None:
            pay = normalize_compensation(
                salary,
                target_currency=profile.salary_currency,
                target_basis=profile.salary_basis,
                fx=fx,
                config=config,
            )
        evaluation = evaluate_eligibility(
            profile,
            bundles,
            compensation=pay,
            config=config,
            evaluated_at=at,
            selected_tier=tiers.get(job.job_id),
        )
        decision = evaluation.decision
        diagnostics = tuple(dict.fromkeys((*notes, *salary_notes, *evaluation.notes)))
        if diagnostics:
            decision = EligibilityDecision(
                criteria=(
                    *decision.criteria,
                    CriterionResult(
                        criterion='input_diagnostics',
                        status='not_applicable',
                        reason_code=diagnostics[0],
                        explanation='; '.join(diagnostics),
                    ),
                )
            )
        results.append(
            score_job(
                job.job_id,
                job.job_version_id,
                profile,
                bundles,
                decision=decision,
                compensation=pay,
                config=config,
                evaluated_at=at,
                selected_tier=tiers.get(job.job_id),
            )
        )
    return tuple(results)
