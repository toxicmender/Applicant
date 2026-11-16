"""Run read-only company browser adapters under parent-owned profile locks."""

from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from ..errors import AuthFailed, Blocked, NotFound, SourceError, Unparseable
from .browser import STATE_FILE, browser_cleanup, browser_state
from .owned_worker import WorkerFailure, run_worker
from .profile_lock import profile_lock
from .ranking_documents import read_ranking_document

_UNSET = object()


class CompanyFetchInterrupted(KeyboardInterrupt):
    def __init__(self, result):
        super().__init__()
        self.result = result


def _fetch(connection, arguments):
    from ..financials.crunchbase import CrunchbaseClient
    from ..financials.tracxn import TracxnClient
    from ..reviews.glassdoor import GlassdoorClient
    from .company_sources import CompanyRecorder

    provider, state, company, limit, headless, timeout, capture = arguments
    recorder = CompanyRecorder() if capture else None
    options = {'profile_dir': None, 'headless': headless, 'timeout': timeout, 'capture': recorder}
    if provider == 'crunchbase':
        adapter = CrunchbaseClient(api_key='', **options)
    elif provider == 'tracxn':
        adapter = TracxnClient(api_key='', **options)
    elif provider == 'glassdoor':
        adapter = GlassdoorClient(**options)
    else:
        raise ValueError('unsupported owned company adapter')
    token = browser_state.set(state)
    try:
        with ExitStack() as cleanup:
            cleanup_token = browser_cleanup.set(cleanup)
            try:
                client = cleanup.enter_context(adapter)
                try:
                    result = client.fetch(company, limit)
                    responses = () if recorder is None else recorder.completed()
                except SourceError as error:
                    return {'error': type(error).__name__}
                connection.send(('progress', (result, responses)))
                return {'error': None}
            finally:
                browser_cleanup.reset(cleanup_token)
                connection.send(('closing', None))
    finally:
        browser_state.reset(token)


@contextmanager
def company_session(provider, client):
    from ..services.provider_setup import _scope_captured_state

    path = Path(client.profile_dir).resolve() / STATE_FILE if client.profile_dir else None
    with profile_lock(path.with_suffix('.lock')) if path is not None else nullcontext():
        state = (
            _scope_captured_state(read_ranking_document(path), provider)
            if path is not None and path.is_file()
            else None
        )
        yield state


def fetch_company(provider, client, company, limit, *, state=_UNSET, check=None, capture=None):
    completed = []

    def received(result):
        value, responses = result
        if capture is not None:
            for response in responses:
                capture(response.source_url, response.body, response.representation)
        completed.append(value)

    access = company_session(provider, client) if state is _UNSET else nullcontext(state)
    try:
        with access as session:
            outcome = run_worker(
                _fetch,
                (
                    provider,
                    session,
                    company,
                    limit,
                    client.headless,
                    client.timeout,
                    capture is not None,
                ),
                progress=received,
                provider=provider,
                check=check,
                timeout=180,
            )
    except KeyboardInterrupt as error:
        if completed:
            raise CompanyFetchInterrupted(completed[-1]) from error
        raise
    except WorkerFailure:
        if not completed:
            raise
        return completed[-1]
    if outcome['error']:
        by_name = {error.__name__: error for error in (AuthFailed, Blocked, NotFound, Unparseable)}
        kind = by_name.get(outcome['error'], SourceError)
        if kind is AuthFailed:
            raise AuthFailed(
                f'{provider} session expired; run applicant setup provider {provider} --reauth'
            )
        raise kind(f'{provider}: acquisition failed')
    if not completed:
        raise Unparseable(f'{provider}: browser returned no completed observation')
    return completed[-1]
