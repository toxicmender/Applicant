"""Authentication is explicit; session secrets never enter research persistence."""

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..domain.canonical import content_hash
from ..domain.contracts import now
from ..domain.providers import provider
from ..errors import ConfigError
from ..files import write_document
from ..infra.browser import BrowserSession, looks_blocked
from ..infra.browser_diagnostics import event, safe_origin, stage
from ..infra.profile_lock import profile_lock
from ..infra.ranking_documents import read_ranking_document
from ..interaction import Terminal

COOKIE_WARNING = (
    'Warning: Cookie-only import lacks local storage state; providers relying on '
    'client-side session tokens may require full Playwright storage-state export.'
)
PROFILES = {
    'glassdoor': 'glassdoor_profile',
    'crunchbase': 'crunchbase_profile',
    'tracxn': 'tracxn_profile',
}

TRACXN_AUTH_WAIT_MS = 10_000


def recovery_database(settings, database):
    from ..infra.store.sqlite import DB_NAME

    if settings.store == 'files':
        if database:
            raise ConfigError('--database cannot be combined with --store files')
        return None
    return (
        Path(settings.path(database))
        if database
        else Path(settings.path(settings.files.listing)).resolve().parent / DB_NAME
    )


def recover_verified(settings, name, database, result):
    from .provider_health import recover_authentication

    path = recovery_database(settings, database)
    result['health_recovery'] = (
        {'status': 'skipped_file_only'} if path is None else recover_authentication(path, name)
    )
    return result


def session_path(settings, name):
    item = provider(name)
    if not item.login_url:
        raise ConfigError(f'{item.id}: session setup is not supported')
    if item.id == 'linkedin':
        return Path(settings.path(settings.files.cookies))
    return Path(settings.path(getattr(settings.files, PROFILES[item.id]))) / 'storage_state.json'


def _provider_root(name: str) -> str:
    return (urlsplit(provider(name).url).hostname or '').removeprefix('www.')


def _provider_host(host: str | None, root: str) -> bool:
    if not isinstance(host, str):
        return False
    host = host.lstrip('.')
    return bool(root) and (host == root or host.endswith('.' + root))


def _scope_captured_state(value, name: str):
    # A browser context also sees cookies and local storage from identity
    # providers and embedded services. Retain only this provider's state.
    root = _provider_root(name)
    cookies, origins = value['cookies'], value['origins']
    scoped_cookies = [cookie for cookie in cookies if _provider_host(cookie['domain'], root)]
    scoped_origins = [
        entry for entry in origins if _provider_host(urlsplit(entry['origin']).hostname, root)
    ]
    event(
        'storage_export',
        'scoped',
        excluded_cookies=len(cookies) - len(scoped_cookies),
        excluded_origins=len(origins) - len(scoped_origins),
    )
    return {**value, 'cookies': scoped_cookies, 'origins': scoped_origins}


def validate_state(value, name, format='playwright'):
    root = _provider_root(name)

    warnings = []
    if format == 'cookies':
        warnings.append({'code': 'cookie_only_storage_incomplete', 'message': COOKIE_WARNING})
        value = {'cookies': value, 'origins': []}
    if not isinstance(value, dict) or set(value) - {'cookies', 'origins'}:
        raise ConfigError(
            'unsupported storage state; supply cookies and origins only (sessionStorage is not included)'
        )
    cookies, origins = value.get('cookies'), value.get('origins', [])
    if not isinstance(cookies, list) or not isinstance(origins, list):
        raise ConfigError('storage state requires cookies and origins arrays')
    normalized = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or set(cookie) - {
            'name',
            'value',
            'domain',
            'path',
            'expires',
            'httpOnly',
            'secure',
            'sameSite',
        }:
            raise ConfigError('unsupported cookie fields')
        if (
            not all(isinstance(cookie.get(k), str) for k in ('name', 'value', 'domain', 'path'))
            or not cookie['name']
            or not cookie['path'].startswith('/')
        ):
            raise ConfigError('cookie has invalid fields')
        if not _provider_host(cookie['domain'], root):
            raise ConfigError('cookie domain is outside the provider allowlist')
        expires = cookie.get('expires', -1)
        if (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(expires)
            or expires < -1
        ):
            raise ConfigError('invalid cookie expiration')
        if any(k in cookie and type(cookie[k]) is not bool for k in ('httpOnly', 'secure')):
            raise ConfigError('invalid cookie boolean')
        if cookie.get('sameSite', 'Lax') not in ('Strict', 'Lax', 'None'):
            raise ConfigError('invalid cookie sameSite')
        normalized.append(
            {'expires': -1, 'httpOnly': False, 'secure': True, 'sameSite': 'Lax', **cookie}
        )
    seen = set()
    for origin in origins:
        if not isinstance(origin, dict) or set(origin) != {'origin', 'localStorage'}:
            raise ConfigError(
                'unsupported origin storage; IndexedDB and sessionStorage imports are not supported'
            )
        if not isinstance(origin['origin'], str):
            raise ConfigError('storage origin must be a string')
        url = urlsplit(origin['origin'])
        if (
            url.scheme != 'https'
            or not _provider_host(url.hostname, root)
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
        ):
            raise ConfigError('invalid storage origin')
        if origin['origin'] in seen:
            raise ConfigError('duplicate storage origin')
        seen.add(origin['origin'])
        entries = origin['localStorage']
        if not isinstance(entries, list) or any(
            not isinstance(x, dict)
            or set(x) != {'name', 'value'}
            or not all(isinstance(v, str) for v in x.values())
            for x in entries
        ):
            raise ConfigError('invalid local storage entries')
        if len({entry['name'] for entry in entries}) != len(entries):
            raise ConfigError('duplicate local storage names')
    if not normalized and not origins:
        raise ConfigError('session contains no authentication state')
    return {'cookies': normalized, 'origins': origins}, warnings


def verify_state(name, state, *, interactive=False, factory: Callable[..., Any] = BrowserSession):
    if not provider(name).auth_selector:
        raise ConfigError('provider has no authentication adapter')
    if factory is BrowserSession:
        from ..infra.owned_worker import run_worker

        return run_worker(
            _verification_worker,
            (name, state, interactive),
            interactive=interactive,
            pause=Terminal().pause,
            provider=name,
        )
    return _verify_inline(name, state, interactive=interactive, factory=factory)


def _verification_worker(connection, arguments):
    name, state, interactive = arguments

    class Session(BrowserSession):
        def __exit__(self, *args):
            connection.send(('closing', None))
            return super().__exit__(*args)

    def pause(message):
        connection.send(('pause', message))
        connection.recv()

    return _verify_inline(name, state, interactive=interactive, factory=Session, pause=pause)


def _verify_inline(
    name, state, *, interactive=False, factory: Callable[..., Any] = BrowserSession, pause=None
):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    item = provider(name)
    if not item.login_url or not item.check_url or not item.auth_selector:
        raise ConfigError('provider has no authentication adapter')
    check_url = item.check_url
    check_origin = safe_origin(check_url)
    check_path = urlsplit(check_url).path

    def on_tracxn_dashboard(url: str) -> bool:
        return safe_origin(url) == check_origin and urlsplit(url).path.rstrip('/') == check_path

    with factory(headless=not interactive, storage_state=state) as session:
        page = session.start()
        if interactive:
            with stage('login_navigation', origin=safe_origin(item.login_url)):
                page.goto(item.login_url, wait_until='domcontentloaded')
            with stage('login_acknowledgement'):
                (pause or Terminal().pause)(
                    'Complete login/MFA in the browser, then press Enter to verify: '
                )
        if item.id == 'tracxn' and interactive:
            # A second navigation can interrupt the login flow's own dashboard redirect.
            with stage('verification_wait', origin=check_origin):
                try:
                    page.wait_for_url(
                        on_tracxn_dashboard, wait_until='commit', timeout=TRACXN_AUTH_WAIT_MS
                    )
                except PlaywrightTimeout:
                    event('verification_wait', 'app_not_reached', origin=safe_origin(page.url))
                    return None
        else:
            with stage('verification_navigation', origin=check_origin):
                wait_until = 'commit' if item.id == 'tracxn' else 'domcontentloaded'
                page.goto(check_url, wait_until=wait_until)
        with stage('authentication_check'):
            if looks_blocked(page):
                event('authentication_check', 'blocked')
                return None
            current_url = page.url
            auth_location = urlsplit(current_url).path if item.id == 'tracxn' else current_url
            if any(
                part in auth_location.lower()
                for part in ('/login', '/authwall', '/checkpoint', '/signin')
            ):
                event('authentication_check', 'login_redirect', origin=safe_origin(page.url))
                return None
            marker = page.locator(item.auth_selector)
            if item.id == 'tracxn':
                if not on_tracxn_dashboard(page.url):
                    event('authentication_check', 'unexpected_page', origin=safe_origin(page.url))
                    return None
                try:
                    marker.first.wait_for(state='attached', timeout=TRACXN_AUTH_WAIT_MS)
                except PlaywrightTimeout:
                    event('authentication_check', 'marker_missing')
                    return None
                if not on_tracxn_dashboard(page.url):
                    event('authentication_check', 'left_app', origin=safe_origin(page.url))
                    return None
            elif not marker.count():
                event('authentication_check', 'marker_missing')
                return None
        with stage('storage_export'):
            return _scope_captured_state(session.context.storage_state(), item.id)


def import_session(
    settings,
    name,
    source,
    *,
    format='playwright',
    dry_run=False,
    replace=False,
    verify=False,
    verifier=verify_state,
    database=None,
):
    recovery_database(settings, database)
    target = session_path(settings, name)
    if Path(source).stat().st_size > 2_000_000:
        raise ConfigError('session file exceeds 2 MB')
    state, warnings = validate_state(read_ranking_document(Path(source)), name, format)
    result = {'provider': provider(name).id, 'status': 'unverified', 'warnings': warnings}
    if dry_run:
        return {**result, 'dry_run': True}
    with profile_lock(target.with_suffix('.lock')):
        if target.exists() and not replace:
            raise ConfigError('session already exists; pass --replace')
        if verify:
            verified = verifier(name, state)
            if verified is None:
                return {
                    **result,
                    'status': 'needs_setup',
                    'command': f'applicant setup provider {name}',
                }
            state, _ = validate_state(verified, name)
            result['status'] = 'verified'
        write_document(target, state)
        write_document(
            target.with_suffix('.status.json'),
            {**result, 'state_hash': content_hash(state), 'checked_at': now().isoformat()},
        )
        if verify:
            recover_verified(settings, name, database, result)
    return result


def setup_provider(settings, name, *, reauth=False, verifier=verify_state, database=None):
    import sys

    recovery_database(settings, database)

    if not sys.stdin.isatty():
        return {
            'provider': name,
            'status': 'needs_setup',
            'command': f'applicant setup import-session --provider {name} --from-file state.json --verify',
        }
    target = session_path(settings, name)
    with profile_lock(target.with_suffix('.lock')):
        if target.exists() and not reauth:
            raise ConfigError('session exists; use status --check or provider --reauth')
        state = verifier(name, None, interactive=True)
        if state is None:
            return {'provider': name, 'status': 'needs_setup'}
        state, warnings = validate_state(state, name)
        write_document(target, state)
        result = {
            'provider': name,
            'status': 'verified',
            'warnings': warnings,
            'checked_at': now().isoformat(),
        }
        write_document(
            target.with_suffix('.status.json'), {**result, 'state_hash': content_hash(state)}
        )
        return recover_verified(settings, name, database, result)


def session_status(settings, name, *, check=False, verifier=verify_state, database=None):
    recovery_database(settings, database)
    target = session_path(settings, name)
    if not target.exists():
        return {'provider': name, 'status': 'needs_setup'}
    with profile_lock(target.with_suffix('.lock')):
        state, _ = validate_state(read_ranking_document(target), name)
        status = 'unverified'
        metadata = target.with_suffix('.status.json')
        if metadata.exists():
            saved = read_ranking_document(metadata)
            if saved.get('state_hash') == content_hash(state):
                status = saved.get('status', 'unverified')
        if check:
            status = 'verified' if verifier(name, state) is not None else 'needs_setup'
            write_document(
                metadata,
                {
                    'state_hash': content_hash(state),
                    'status': status,
                    'checked_at': now().isoformat(),
                },
            )
        result = {'provider': name, 'status': status, 'network_checked': check}
        return (
            recover_verified(settings, name, database, result)
            if check and status == 'verified'
            else result
        )
