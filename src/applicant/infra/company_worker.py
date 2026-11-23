"""Run read-only company browser adapters under parent-owned profile locks."""

from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path

from ..errors import AuthFailed, Blocked, NotFound, SourceError, Unparseable
from .browser import STATE_FILE, browser_cleanup, browser_state
from .owned_worker import WorkerFailure, run_worker
from .profile_lock import profile_lock
from .session_access import SessionExpired, run_with_session
from .session_store import SessionStore, read_session_file

_UNSET = object()


class CompanyFetchInterrupted(KeyboardInterrupt):
    def __init__(self, result):
        super().__init__()
        self.result = result


def _fetch(connection, arguments):
    if len(arguments) == 7:
        return _fetch_once(connection, arguments)
    *request, configuration = arguments
    path, provider, mode, dedicated = configuration
    store = SessionStore(path, provider, mode=mode, dedicated=dedicated)
    try:
        result, access = run_with_session(
            store,
            lambda: _fetch_once(None, request),
            public=True,
        )
        if progress := result.pop('result', None):
            connection.send(('progress', progress))
        return {**result, 'access': access}
    finally:
        connection.send(('closing', None))


def _fetch_once(connection, arguments):
    from ..financials.crunchbase import CrunchbaseClient
    from ..financials.tracxn import TracxnClient
    from ..reviews.ambitionbox import AmbitionBoxClient
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
    elif provider == 'ambitionbox':
        adapter = AmbitionBoxClient(capture=recorder)
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
                except SessionExpired:
                    raise
                except SourceError as error:
                    return {'error': type(error).__name__}
                if connection is None:
                    return {'error': None, 'result': (result, responses)}
                connection.send(('progress', (result, responses)))
                return {'error': None}
            finally:
                browser_cleanup.reset(cleanup_token)
                if connection is not None:
                    connection.send(('closing', None))
    finally:
        browser_state.reset(token)


@contextmanager
def company_session(provider, client):
    from ..services.provider_setup import _scope_captured_state

    path = (
        Path(client.profile_dir).resolve() / STATE_FILE
        if getattr(client, 'profile_dir', None)
        else None
    )
    with profile_lock(path.with_suffix('.lock')) if path is not None else nullcontext():
        state = read_session_file(path, expected_provider=provider) if path is not None else None
        yield _scope_captured_state(state, provider) if state is not None else None


def fetch_company(provider, client, company, limit, *, state=_UNSET, check=None, capture=None):
    completed = []

    def received(result):
        value, responses = result
        if capture is not None:
            for response in responses:
                capture(response.source_url, response.body, response.representation)
        completed.append(value)

    managed = getattr(client, 'session_store', None)
    use_managed = managed is not None and managed.path.is_file()
    if provider == 'glassdoor' and use_managed and managed is not None:
        from ..reviews.glassdoor import reviews_url

        record = managed.snapshot()
        region = record.get('region') if record.get('status') != 'cleared' else None
        company = reviews_url(company, region=region)[0]
    access = (
        nullcontext(None)
        if use_managed
        else company_session(provider, client)
        if state is _UNSET
        else nullcontext(state)
    )
    try:
        with access as session:
            request = (
                provider,
                session,
                company,
                limit,
                getattr(client, 'headless', True),
                getattr(client, 'timeout', 45_000),
                capture is not None,
            )
            if use_managed and managed is not None:
                request = (*request, (managed.path, provider, managed.mode, managed.dedicated))
            outcome = run_worker(
                _fetch,
                request,
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
    if managed is not None:
        client.access = outcome.get('access', {'access_mode': 'public'})
        if client.access.get('reason') == 'session_expired':
            from ..log import get

            get(__name__).warning(
                '%s: retry used public access. %s', provider, client.access['command']
            )
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
