import json
from unittest import mock

from applicant.infra.company_worker import _fetch
from applicant.infra.profile_lock import profile_lock
from tests.fakes import FakePage, Visit


def apollo_html(state):
    return '<script>window.appCache={"apolloState":' + json.dumps(state) + '};</script>'


def glassdoor_fixture_worker(connection, arguments):
    from applicant.infra.browser import BrowserSession
    from applicant.infra.session_access import active_access

    def start(session):
        state = session.storage_state
        private = bool(state and state['cookies'])
        if private:
            assert active_access.get() is not None
        page = FakePage(
            [(r'E42\.htm$', Visit(html=glassdoor_fixture_body(private), status=arguments[-1]))]
        )
        context = mock.Mock()
        if private:
            rotated = {'cookies': [{**state['cookies'][0], 'value': 'rotated'}], 'origins': []}
            context.storage_state.return_value = rotated
        session._context = context
        return page

    with mock.patch.object(BrowserSession, 'start', start):
        return _fetch(connection, arguments[:-1])


def glassdoor_fixture_body(private):
    employer = {'id': 42, 'ratingOverall': 4.2, 'reviewCount': 12}
    if private:
        employer['account'] = 'PRIVATE_CANARY'
    return apollo_html({'employer': employer})


def hold_lock(path, ready, release):
    with profile_lock(path):
        ready.set()
        if not release.wait(30):
            raise RuntimeError('test did not release the profile lock')
