"""Pure versioned reports for completed engine runs, with display-only limits."""

from .ranking import JobVersion, RankingRun, ScoringConfiguration
from .ranking_v2 import RankingRunV2, RankingRunV3, ScoringConfigurationV2
from .ranking_views import ranked_views


def _company_context(job: JobVersion) -> dict:
    records = [
        e.payload for e in job.evidence if e.payload.get('kind') == 'company_evidence_context'
    ]
    if not records:
        return {}
    return {
        'company_evidence': [
            {k: v for k, v in record.items() if k != 'responses'} for record in records
        ]
    }


def ranking_report(
    run: RankingRun | RankingRunV2 | RankingRunV3,
    jobs: tuple[JobVersion, ...],
    *,
    config: ScoringConfiguration | ScoringConfigurationV2,
    top: int | None = 20,
) -> dict:
    versions = {job.job_version_id: job for job in jobs}
    if isinstance(run, RankingRun):
        results = {r.job_version_id: r for r in run.results}
        ids = run.job_version_ids if top is None else run.job_version_ids[:top]
        return {
            'kind': 'ranking_report',
            'schema_version': 1,
            'run': run.model_dump(exclude={'results'}),
            'legacy': {
                'notice': 'eligibility unavailable in this format',
                'total': len(run.job_version_ids),
                'displayed': len(ids),
                'results': [
                    {
                        'job': versions[key].job_data,
                        'job_version_id': key,
                        'ranking': results[key].model_dump() if key in results else None,
                    }
                    for key in ids
                ],
            },
        }
    if not isinstance(config, ScoringConfigurationV2):
        raise ValueError('engine report requires engine configuration')
    views = ranked_views(run.results, config=config)
    groups = {}
    for state in ('eligible', 'review_required', 'excluded'):
        results = getattr(views, state)
        displayed = results if top is None else results[:top]
        groups[state] = {
            'total': len(results),
            'displayed': len(displayed),
            'results': [
                result.model_dump()
                | {
                    'eligibility': result.eligibility,
                    'job': versions[result.job_version_id].job_data,
                    **_company_context(versions[result.job_version_id]),
                }
                for result in displayed
            ],
        }
    return {
        'kind': 'ranking_report',
        'schema_version': 1,
        'run': run.model_dump(exclude={'results'}),
        'groups': groups,
    }
