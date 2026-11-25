from argparse import Namespace

from applicant.domain.job import Job
from applicant.infra.owned_worker import CleanupUnconfirmed
from applicant.services.research import read_research
from applicant.services.research_feed import collect_feed


def test_unconfirmed_cleanup_keeps_committed_sibling_only(tmp_path):
    def factory(name, **kwargs):
        job = Job(id='example', source=name, title='Engineer', url=f'https://example.test/{name}')
        if name == 'weworkremotely':

            def search(*args, **kwargs):
                raise CleanupUnconfirmed('owned tree could not be confirmed')
        else:

            def search(*args, **kwargs):
                return [job]

        return Namespace(search=search, close=lambda: None, partial_jobs=[job])

    database = tmp_path / 'applicant.db'
    result = collect_feed(
        database,
        {
            'providers': ['wellfound', 'weworkremotely', 'builtin'],
        },
        factory=factory,
    )
    assert result['outcome'] == 'partial_commit'
    assert result['committed_providers'] == ['wellfound']
    assert result['failed_provider'] == 'weworkremotely'
    assert result['skipped_providers'] == ['builtin']
    assert [item.job.source for item in read_research(database)['observations']] == ['wellfound']
