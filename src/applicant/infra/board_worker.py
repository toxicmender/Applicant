"""Run production acquisition inside a cancellable owned process tree."""

from collections.abc import Callable
from pathlib import Path

from ..domain.job import Job
from ..errors import (
    AuthFailed,
    Blocked,
    NotFound,
    RateLimited,
    SourceError,
    Unparseable,
    Unreachable,
)
from .owned_worker import run_worker
from .provider_registry import board_class, board_client
from .session_access import SessionExpired, run_with_session

_SESSION_BOARDS = {'indeed', 'naukri', 'googlejobs'}


def _collect(connection, request):
    if len(request) <= 5:
        return _collect_once(connection, request)
    from ..services.provider_setup import provider_session
    from ..settings import Settings

    *arguments, configured = request
    name = arguments[0]
    try:
        result, access = run_with_session(
            provider_session(Settings.model_validate(configured), name),
            lambda: _collect_once(None, arguments),
            public=True,
        )
        result['access'] = access
        if result.get('jobs'):
            connection.send(('progress', result))
        return result
    finally:
        connection.send(('closing', None))


def _collect_once(connection, request):
    name, method, args, kwargs = request[:4]
    headless = request[4] if len(request) == 5 else True
    client = None
    jobs = []
    failure = None
    try:
        client = board_client(name, headless=headless)
        if method == 'describe':
            return {'description': client.describe(Job.model_validate(args[0]))}
        jobs = getattr(client, method)(*args, **kwargs)
    except SessionExpired:
        raise
    except SourceError as error:
        jobs = getattr(client, 'partial_jobs', [])
        failure = {'type': type(error).__name__, 'retry_at': getattr(error, 'retry_at', None)}
    finally:
        try:
            result = {
                'jobs': [job.model_dump() for job in jobs],
                'error': failure,
                'raw': getattr(client, 'raw', None),
                'source_urls': getattr(client, 'source_urls', {}),
                'requisition_metadata': getattr(client, 'requisition_metadata', {}),
            }
            if jobs and connection is not None:
                connection.send(('progress', result))
        finally:
            if connection is not None:
                connection.send(('closing', None))
            if client is not None:
                client.close()
    return result


class OwnedBoard:
    def __init__(self, name, check=None, *, headless=True, settings=None):
        self.name, self.check = name, check
        self.headless = headless
        self.settings = settings
        self.access = {}
        self.raw, self.source_urls, self.requisition_metadata = {}, {}, {}
        self.partial_jobs = []

    def _call(self, method, *args, **kwargs):
        self.partial_jobs = []
        request = (self.name, method, args, kwargs)
        if self.settings is not None and self.name in _SESSION_BOARDS:
            request = (
                *request,
                self.headless,
                self.settings.model_dump(
                    include={
                        'data_dir',
                        'files',
                        'store',
                        'session_storage',
                    }
                ),
            )
        elif not self.headless:
            request = (*request, False)
        result = run_worker(
            _collect,
            request,
            timeout=None,
            check=self.check,
            progress=self._receive if method != 'describe' else None,
            provider=self.name,
        )
        self.access = result.get('access', {})
        if self.access.get('reason') == 'session_expired':
            from ..log import get

            get(__name__).warning(
                '%s: retry used public access. %s', self.name, self.access['command']
            )
        if method == 'describe' and 'description' in result:
            return result['description']
        jobs = self._receive(result)
        if result['error']:
            error = result['error']
            classes = {
                c.__name__: c
                for c in (AuthFailed, Blocked, NotFound, RateLimited, Unparseable, Unreachable)
            }
            cls = classes.get(error['type'], SourceError)
            if cls is RateLimited:
                raise RateLimited('rate limit', retry_at=error['retry_at'])
            raise cls('provider acquisition failed')
        self.partial_jobs = []
        return jobs

    def _receive(self, result):
        if isinstance(self.raw, dict) and isinstance(result['raw'], dict):
            self.raw.update(result['raw'])
        else:
            self.raw = result['raw']
        self.source_urls.update(result['source_urls'])
        self.requisition_metadata.update(result['requisition_metadata'])
        self.partial_jobs = [Job.model_validate(job) for job in result['jobs']]
        return self.partial_jobs

    def search(self, *args, **kwargs):
        return self._call('search', *args, **kwargs)

    @property
    def capability(self):
        return board_class(self.name).capability

    def __getattr__(self, name):
        if name == 'describe' and callable(getattr(board_class(self.name), name, None)):
            return lambda job: self._call('describe', job.model_dump(mode='json'))
        raise AttributeError(name)

    def capture(self, url):
        return self._call('capture', url)

    def close(self):
        pass


def _recommendations(connection, arguments):
    from ..boards.linkedin_apply import LinkedIn

    state, headless = arguments
    client = LinkedIn(headless=headless)
    try:
        try:
            jobs = client.scrape_jobs(None, storage_state=state)
        except Blocked:
            return {'status': 'needs_setup', 'jobs': []}
        value = {'status': 'success', 'jobs': [job.model_dump() for job in jobs]}
        connection.send(('progress', value))
        return value
    finally:
        connection.send(('closing', None))
        client.close()


def recommendations(
    session_path: Path, *, headless: bool, save: Callable[[list[Job]], int]
) -> list[Job]:
    from ..services.provider_setup import _scope_captured_state
    from .profile_lock import profile_lock
    from .session_store import read_session_file

    results: list[Job] = []

    def progress(value):
        results.extend(Job.model_validate(job) for job in value['jobs'])

    try:
        with profile_lock(session_path.with_suffix('.lock')):
            if not session_path.is_file():
                raise AuthFailed('LinkedIn needs setup')
            state = read_session_file(session_path, expected_provider='linkedin')
            if not isinstance(state, dict) or not isinstance(state.get('cookies'), list):
                raise AuthFailed('LinkedIn needs setup')
            state = _scope_captured_state({'origins': [], **state}, 'linkedin')
            if not state['cookies'] and not state['origins']:
                raise AuthFailed('LinkedIn needs setup')
            result = run_worker(
                _recommendations, (state, headless), progress=progress, provider='linkedin'
            )
            if result['status'] == 'needs_setup':
                raise AuthFailed('LinkedIn needs setup')
    finally:
        if results:
            save(results)
    return results
