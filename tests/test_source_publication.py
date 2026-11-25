from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

from applicant.domain.job import Job
from applicant.infra.owned_worker import CleanupUnconfirmed
from applicant.services.research import read_research
from applicant.services.research_feed import collect_feed
from applicant.settings import Settings


def test_unconfirmed_cleanup_keeps_committed_sibling_only(tmp_path):
    def factory(name, **kwargs):
        job = Job(id='example', source=name, title='Engineer', url=f'https://example.test/{name}')

        def search(*args, **kwargs):
            if name == 'weworkremotely':
                raise CleanupUnconfirmed('owned tree could not be confirmed')
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


def test_normal_feed_dispatch_respects_route_policy_and_credentials(tmp_path):
    launches = []

    class OwnedFixture:
        def __init__(self, name, check, *, settings=None):
            self.name = name
            self.access = {'access_mode': 'authenticated' if settings else 'public'}
            launches.append((name, bool(settings)))

        def search(self, *_args, **_kwargs):
            return [
                Job(id='one', source=self.name, title='Engineer', url='https://example.test/job')
            ]

        def close(self):
            pass

    settings = Settings(data_dir=tmp_path)
    with (
        patch('applicant.services.research_feed.OwnedBoard', OwnedFixture),
        patch(
            'applicant.services.provider_setup.provider_session',
            return_value=SimpleNamespace(snapshot=lambda: {'status': 'verified'}),
        ),
    ):
        result = collect_feed(
            tmp_path / 'sources.db',
            {'providers': ['arc', 'indeed', 'weworkremotely']},
            settings=settings,
        )
    assert launches == [('indeed', True), ('weworkremotely', False)]
    assert [x['status'] for x in result['outcomes']] == ['prohibited', 'success', 'success']
