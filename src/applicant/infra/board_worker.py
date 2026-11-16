"""Run production acquisition inside a cancellable owned process tree."""

from collections.abc import Callable
from pathlib import Path

from ..domain.job import Job
from ..errors import AuthFailed, Blocked, RateLimited, SourceError, Unparseable, Unreachable
from .owned_worker import run_worker
from .provider_registry import board_class, board_client


def _collect(connection, request):
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
            if jobs:
                connection.send(('progress', result))
        finally:
            connection.send(('closing', None))
            if client is not None:
                client.close()
    return result


class OwnedBoard:
    def __init__(self, name, check=None, *, headless=True):
        self.name, self.check = name, check
        self.headless = headless
        self.raw, self.source_urls, self.requisition_metadata = {}, {}, {}
        self.partial_jobs = []

    def _call(self, method, *args, **kwargs):
        self.partial_jobs = []
        result = run_worker(
            _collect,
            (self.name, method, args, kwargs)
            if self.headless
            else (self.name, method, args, kwargs, False),
            timeout=None,
            check=self.check,
            progress=self._receive if method != 'describe' else None,
            provider=self.name,
        )
        if method == 'describe' and 'description' in result:
            return result['description']
        jobs = self._receive(result)
        if result['error']:
            error = result['error']
            classes = {
                c.__name__: c for c in (AuthFailed, Blocked, RateLimited, Unparseable, Unreachable)
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
    from .ranking_documents import read_ranking_document

    results: list[Job] = []

    def progress(value):
        results.extend(Job.model_validate(job) for job in value['jobs'])

    try:
        with profile_lock(session_path.with_suffix('.lock')):
            if not session_path.is_file():
                raise AuthFailed('LinkedIn needs setup')
            state = read_ranking_document(session_path)
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
