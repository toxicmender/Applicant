"""Descriptive ranking evaluation with explicit unknown judgments."""

from decimal import Decimal, localcontext

from .canonical import content_hash
from .feedback import FeedbackEvent
from .ranking_math import arithmetic_context
from .ranking_v2 import RankingRunV2, RankingRunV3, ScoringConfigurationV2
from .ranking_views import ranked_views


def evaluate_feedback(
    run: RankingRunV2 | RankingRunV3,
    config: ScoringConfigurationV2,
    events: tuple[FeedbackEvent, ...],
    *,
    top: int = 20,
    through: int,
) -> dict:
    if top <= 0 or through < 0:
        raise ValueError('top must be positive and feedback cutoff nonnegative')
    members = {r.job_id: r.job_version_id for r in run.results}
    effective = {}
    for event in events:
        if (
            event.request.run_id != run.ranking_run_id
            or event.sequence > through
            or members.get(event.request.job_id) != event.job_version_id
            or (event.profile_id, event.profile_revision) != (run.profile_id, run.profile_revision)
        ):
            raise ValueError('feedback does not belong to the exact evaluation inputs')
        effective[event.request.job_id] = event
    views = ranked_views(run.results, config=config)

    def counts(results):
        decisions = [
            effective[r.job_id].request.decision if r.job_id in effective else None for r in results
        ]
        return {
            'candidates': len(results),
            'judged': sum(d in ('shortlist', 'dismiss') for d in decisions),
            'shortlisted': decisions.count('shortlist'),
            'dismissed': decisions.count('dismiss'),
        }

    total = counts(views.eligible)
    displayed = counts(views.eligible[:top])
    with localcontext(arithmetic_context()):

        def ratio(n, d):
            return Decimal(n) / Decimal(d) if d else None

        n = displayed['candidates']
        full = displayed['judged'] == n
        metrics = {
            'judgment_coverage': ratio(total['judged'], total['candidates']),
            'shortlist_fraction_among_judged': ratio(total['shortlisted'], total['judged']),
            'precision_at_k': ratio(displayed['shortlisted'], n) if full else None,
            'precision_lower_bound': ratio(displayed['shortlisted'], n),
            'precision_upper_bound': ratio(displayed['shortlisted'] + n - displayed['judged'], n),
            'recall_at_k': ratio(displayed['shortlisted'], total['shortlisted'])
            if total['judged'] == total['candidates']
            else None,
        }
    inputs = {
        'run': run,
        'configuration': config,
        'events': events,
        'top': top,
        'through': through,
        'metric_policy': 'explicit-judgments-v1',
    }
    return {
        'kind': 'ranking_evaluation',
        'schema_version': 1,
        'metric_policy': 'explicit-judgments-v1',
        'run_id': run.ranking_run_id,
        'feedback_through': through,
        'event_ids': [e.request.event_id for e in events],
        'input_digest': content_hash(inputs),
        'requested_k': top,
        'effective_k': n,
        'eligible': total,
        'displayed': displayed,
        'metrics': metrics,
        'other_groups': {
            state: counts(getattr(views, state)) for state in ('review_required', 'excluded')
        },
        'notice': 'Descriptive explicit judgments; unjudged jobs are unknown. Selection bias limits conclusions.',
    }


def compare_runs(base: dict, candidate: dict) -> dict:
    with localcontext(arithmetic_context()):
        return _compare_runs(base, candidate)


def _compare_runs(base: dict, candidate: dict) -> dict:
    if base['run']['profile_id'] != candidate['run']['profile_id']:
        raise ValueError('comparison requires the same profile ID')

    def indexed(report):
        if 'legacy' in report:
            return {
                item['job_id']: (None, i + 1, item['ranking'], item['job_version_id'])
                for i, item in enumerate(report['legacy']['results'])
            }
        return {
            r['job_id']: (state, i + 1, r, r['job_version_id'])
            for state, group in report['groups'].items()
            for i, r in enumerate(group['results'])
        }

    # Legacy membership must be joined using job identity supplied by the service.
    left, right = indexed(base), indexed(candidate)
    rows = []
    for job_id in sorted(left.keys() | right.keys()):
        a, b = left.get(job_id), right.get(job_id)
        av = a[3] if a else None
        bv = b[3] if b else None
        changes = {}
        for field in ('overall_score', 'confidence', 'coverage'):
            x = a[2].get(field) if a and a[2] else None
            y = b[2].get(field) if b and b[2] else None
            changes[field] = {
                'base': x,
                'candidate': y,
                'delta': y - x if x is not None and y is not None else None,
            }
        rows.append(
            {
                'job_id': job_id,
                'membership': 'added'
                if a is None
                else 'removed'
                if b is None
                else 'unchanged_version'
                if av == bv
                else 'changed_version',
                'base_group': a[0] if a else None,
                'candidate_group': b[0] if b else None,
                'base_position': a[1] if a and a[0] else None,
                'candidate_position': b[1] if b and b[0] else None,
                'base_result': a[2] if a else None,
                'candidate_result': b[2] if b else None,
                'changes': changes,
            }
        )
    return {
        'kind': 'ranking_comparison',
        'schema_version': 1,
        'base': base['run'],
        'candidate': candidate['run'],
        'jobs': rows,
        'changed_run_inputs': [
            k
            for k in sorted(base['run'].keys() | candidate['run'].keys())
            if k not in ('ranking_run_id', 'calculated_at', 'job_version_ids')
            and base['run'].get(k) != candidate['run'].get(k)
        ],
        'notice': 'Changed inputs are descriptive differences; score changes are not attributed solely to weights.',
    }
