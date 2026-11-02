"""Pure versioned reports for completed engine runs, with display-only limits."""

from .ranking import JobVersion
from .ranking_v2 import RankingRunV3, ScoringConfigurationV2
from .ranking_views import ranked_views


def ranking_report(
    run: RankingRunV3,
    jobs: tuple[JobVersion, ...],
    *,
    config: ScoringConfigurationV2,
    top: int | None = 20,
) -> dict:
    views = ranked_views(run.results, config=config)
    versions = {job.job_version_id: job for job in jobs}
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
