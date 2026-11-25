import json

import httpx
import pytest

from applicant.boards.public import PublicBoard
from applicant.errors import Unparseable


def posting(identifier, title):
    return (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                '@type': 'JobPosting',
                'identifier': identifier,
                'title': title,
                'hiringOrganization': {'name': 'Example Corp'},
            }
        )
        + '</script>'
    )


def test_search_filters_before_exhausting_result_limit():
    seen = []

    def respond(request):
        seen.append(request.url.path)
        pages = {
            '/jobs': '<a href="/jobs/1">One</a><a href="/jobs/2">Two</a>',
            '/jobs/1': posting('1', 'Support'),
            '/jobs/2': posting('2', 'Engineer'),
        }
        return httpx.Response(200, text=pages[request.url.path])

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        jobs = PublicBoard('wellfound', client=http).search('Engineer', limit=1)
    assert [job.id for job in jobs] == ['2']
    assert seen == ['/jobs', '/jobs/1', '/jobs/2']


def test_off_origin_redirect_is_rejected_before_request():
    seen = []

    def respond(request):
        seen.append(request.url.host)
        if request.url.host == 'wellfound.com':
            return httpx.Response(302, headers={'Location': 'https://example.test/jobs/1'})
        return httpx.Response(200, text=posting('1', 'Engineer'))

    with (
        httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=True) as http,
        pytest.raises(Unparseable, match='outside'),
    ):
        PublicBoard('wellfound', client=http).search('Engineer')
    assert seen == ['wellfound.com']


def test_off_origin_candidate_cannot_abort_valid_discovery():
    def respond(request):
        if request.url.path == '/jobs':
            return httpx.Response(
                200,
                text=('<a href="https://example.test/jobs/1">Other</a><a href="/jobs/2">Two</a>'),
            )
        assert request.url.host == 'wellfound.com'
        return httpx.Response(200, text=posting('2', 'Engineer'))

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        jobs = PublicBoard('wellfound', client=http).search('Engineer', limit=1)
    assert [job.id for job in jobs] == ['2']
