"""Run production acquisition inside a cancellable owned process tree."""

from ..domain.job import Job
from ..errors import AuthFailed, Blocked, RateLimited, SourceError, Unparseable, Unreachable
from .owned_worker import run_worker
from .provider_registry import board_client


def _collect(connection, request):
    name, method, args, kwargs = request
    client = None
    jobs = []
    failure = None
    try:
        client = board_client(name, headless=True)
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
    def __init__(self, name, check):
        self.name, self.check = name, check
        self.raw, self.source_urls, self.requisition_metadata = {}, {}, {}
        self.partial_jobs = []

    def _call(self, method, *args, **kwargs):
        self.partial_jobs = []
        result = run_worker(
            _collect,
            (self.name, method, args, kwargs),
            timeout=None,
            check=self.check,
            progress=self._receive,
            provider=self.name,
        )
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

    def capture(self, url):
        return self._call('capture', url)

    def close(self):
        pass
