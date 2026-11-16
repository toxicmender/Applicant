"""Run read-only company browser adapters under parent-owned profile locks."""

from contextlib import nullcontext
from pathlib import Path

from ..errors import AuthFailed, Blocked, NotFound, SourceError, Unparseable
from .browser import STATE_FILE, browser_state
from .owned_worker import WorkerFailure, run_worker
from .profile_lock import profile_lock
from .ranking_documents import read_ranking_document


class CompanyFetchInterrupted(KeyboardInterrupt):
    def __init__(self, result):
        super().__init__()
        self.result = result


def _fetch(connection, arguments):
    from ..financials.crunchbase import CrunchbaseClient
    from ..reviews.glassdoor import GlassdoorClient

    provider, state, company, limit, headless, timeout = arguments
    adapter = (
        CrunchbaseClient(api_key='', profile_dir=None, headless=headless, timeout=timeout)
        if provider == 'crunchbase'
        else GlassdoorClient(profile_dir=None, headless=headless, timeout=timeout)
    )
    token = browser_state.set(state)
    try:
        with adapter as client:
            try:
                try:
                    result = client.fetch(company, limit)
                except SourceError as error:
                    return {'error': type(error).__name__}
                connection.send(('progress', result))
                return {'error': None}
            finally:
                connection.send(('closing', None))
    finally:
        browser_state.reset(token)


def fetch_company(provider, client, company, limit):
    from ..services.provider_setup import _scope_captured_state

    path = Path(client.profile_dir).resolve() / STATE_FILE if client.profile_dir else None
    completed = []
    try:
        with profile_lock(path.with_suffix('.lock')) if path is not None else nullcontext():
            state = None
            if path is not None and path.is_file():
                state = _scope_captured_state(read_ranking_document(path), provider)
            outcome = run_worker(
                _fetch,
                (provider, state, company, limit, client.headless, client.timeout),
                progress=completed.append,
                provider=provider,
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
        raise by_name.get(outcome['error'], SourceError)(f'{provider}: acquisition failed')
    if not completed:
        raise Unparseable(f'{provider}: browser returned no completed observation')
    return completed[-1]
