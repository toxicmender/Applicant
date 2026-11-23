"""Authentication is explicit; session secrets never enter research persistence."""

import math
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

from ..domain.canonical import content_hash
from ..domain.contracts import now
from ..domain.providers import provider
from ..errors import AuthFailed, ConfigError, ProfileBusy
from ..infra.browser import BrowserSession, looks_blocked
from ..infra.browser_diagnostics import event, safe_origin, stage
from ..infra.owned_worker import CleanupUnconfirmed, WorkerFailure
from ..infra.path_identity import physical_path
from ..infra.ranking_documents import read_ranking_document
from ..infra.session_store import FORMAT, SessionStore
from ..interaction import Terminal
from ..settings import Settings

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


def reconcile_session(settings, store, database, result):
    from .provider_health import recover_authentication

    path = recovery_database(settings, database)

    def action(pending, saved_database):
        if path is None:
            return {'status': 'skipped_file_only'}
        policy = provider(store.provider).session
        if pending == 'expire' and policy is not None and policy.public_access:
            return {'status': 'credential_needs_setup'}
        return recover_authentication(saved_database or path, store.provider, action=pending)

    current, outcome = store.reconcile(action)
    if current.get('status') == 'cleared' and result['status'] != 'cleared':
        return {'provider': store.provider, 'status': 'needs_setup', 'health_recovery': outcome}
    return {**result, 'health_recovery': outcome}


def session_path(settings, name):
    item = provider(name)
    if not item.session:
        raise ConfigError(f'{item.id}: session setup is not supported')
    if item.id == 'linkedin':
        return Path(settings.path(settings.files.cookies))
    if item.id in PROFILES:
        return (
            Path(settings.path(getattr(settings.files, PROFILES[item.id]))) / 'storage_state.json'
        )
    return Path(settings.path(f'.applicant/sessions/{item.id}/storage_state.json'))


def _session_policy(name):
    policy = provider(name).session
    if policy is None:
        raise ConfigError(f'{name}: session setup is not supported')
    return policy


def _provider_domains(name: str) -> tuple[str, ...]:
    item = provider(name)
    return item.session.storage_domains if item.session else ()


def _allowed_domain(host: str | None, domains: tuple[str, ...]) -> bool:
    return any(_provider_host(host, root) for root in domains)


def _provider_host(host: str | None, root: str) -> bool:
    if not isinstance(host, str):
        return False
    host = host.lstrip('.')
    return bool(root) and (host == root or host.endswith('.' + root))


def _scope_captured_state(value, name: str):
    # A browser context also sees cookies and local storage from identity
    # providers and embedded services. Retain only this provider's state.
    domains = _provider_domains(name)
    cookies, origins = value['cookies'], value['origins']
    scoped_cookies = [cookie for cookie in cookies if _allowed_domain(cookie['domain'], domains)]
    scoped_origins = [
        entry for entry in origins if _allowed_domain(urlsplit(entry['origin']).hostname, domains)
    ]
    event(
        'storage_export',
        'scoped',
        excluded_cookies=len(cookies) - len(scoped_cookies),
        excluded_origins=len(origins) - len(scoped_origins),
    )
    return {**value, 'cookies': scoped_cookies, 'origins': scoped_origins}


def validate_state(value, name, format='playwright'):
    domains = _provider_domains(name)

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
        if not _allowed_domain(cookie['domain'], domains):
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
            or not _allowed_domain(url.hostname, domains)
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
            prompt=Terminal().ask_interruptibly if interactive else None,
            provider=name,
        )
    return _verify_inline(name, state, interactive=interactive, factory=factory)


def _verification_worker(connection, arguments):
    from ..infra.owned_worker import WorkerInteraction

    name, state, interactive, *options = arguments
    current = None

    class Session(BrowserSession):
        def start(self):
            nonlocal current
            current = super().start()
            return current

        def __exit__(self, *args):
            connection.send(('closing', None))
            return super().__exit__(*args)

    def check():
        if current is None or current.is_closed():
            raise AuthFailed('browser closed during login')
        current.wait_for_timeout(50)

    interaction = WorkerInteraction(connection, check, AuthFailed('login cancelled'))
    if options:
        return _verify_detailed(
            name,
            state,
            factory=Session,
            pause=interaction.pause,
            interactive=interactive,
            **options[0],
        )
    return _verify_inline(
        name, state, interactive=interactive, factory=Session, pause=interaction.pause
    )


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


def _same_route(actual, expected):
    url, target = urlsplit(actual), urlsplit(expected)
    return (
        url.scheme == 'https'
        and url.netloc == target.netloc
        and url.path.rstrip('/') == target.path.rstrip('/')
    )


def _visible(page, selector):
    return page.locator(f':is({selector}):visible').first.is_visible()


def _is_signed_out(name, url):
    policy = provider(name).session
    assert policy
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not _allowed_domain(parsed.hostname, policy.storage_domains):
        return False
    return bool(
        set(parsed.path.lower().split('/'))
        & {
            'login',
            'nlogin',
            'authwall',
            'signin',
            'sign-in',
            'login_input.htm',
            'servicelogin',
            'sign_in',
        }
    )


def _verify_detailed(
    name,
    state,
    *,
    interactive=False,
    show=False,
    region=None,
    capture=None,
    origin=None,
    factory: Callable[..., Any] = BrowserSession,
    pause=None,
):
    policy = provider(name).session
    if policy is None:
        raise ConfigError('provider has no authentication adapter')
    targets = [t for t in policy.targets if not region or t.region == region]
    if not targets:
        raise ConfigError(f'{name}: unsupported verification region')
    with factory(
        headless=not (interactive or show), storage_state=state, capture_settings=capture
    ) as session:
        page = session.start()
        if interactive:
            login = policy.login
            if name == 'glassdoor' and region == 'com':
                login = 'https://www.glassdoor.com/profile/login_input.htm'
            page.goto(login, wait_until='domcontentloaded')
            (pause or Terminal().pause)(
                'Complete login/MFA in the browser, then press Enter to verify: '
            )
        current = next((t for t in targets if _same_route(page.url, t.url)), None)
        if current is None and interactive and name == 'tracxn':
            from playwright.sync_api import TimeoutError as PlaywrightTimeout

            try:
                page.wait_for_url(
                    lambda url: _same_route(url, targets[0].url),
                    wait_until='commit',
                    timeout=TRACXN_AUTH_WAIT_MS,
                )
            except PlaywrightTimeout:
                return {'status': 'inconclusive', 'reason': 'unexpected_page'}
            current = targets[0]
        if current is not None:
            return _inspect_verification(name, session, page, targets)
        attempts = targets[:1]
        unconfirmed_region = False
        if name == 'indeed' and state:
            preferred = [target for target in targets if safe_origin(target.url) == origin]
            hosts = {
                cookie['domain']
                for cookie in state['cookies']
                if not cookie['domain'].startswith('.')
            }
            hosts.update(urlsplit(item['origin']).hostname for item in state['origins'])
            hinted = [target for target in targets if urlsplit(target.url).hostname in hosts]
            attempts = preferred or hinted or attempts
            unconfirmed_region = not (preferred or hinted)
        if name == 'glassdoor' and state and not region:
            domains = {c['domain'].lstrip('.') for c in state['cookies']}
            domains.update(urlsplit(o['origin']).hostname or '' for o in state['origins'])
            hinted = [
                target
                for target in targets
                if any(_provider_host(domain, f'glassdoor.{target.region}') for domain in domains)
            ]
            preferred = hinted[0].region if len(hinted) == 1 else 'com'
            attempts = sorted(targets, key=lambda target: target.region != preferred)
        inconclusive = None
        for target in attempts:
            page.goto(target.url, wait_until='domcontentloaded')
            outcome = _inspect_verification(name, session, page, targets)
            if outcome['status'] == 'verified' or outcome.get('reason') == 'challenge':
                return outcome
            if name == 'indeed' and outcome['status'] == 'needs_setup':
                final_origin = safe_origin(page.url)
                if final_origin != safe_origin(target.url) and any(
                    safe_origin(candidate.url) == final_origin for candidate in targets
                ):
                    outcome = {'status': 'inconclusive', 'reason': 'unexpected_page'}
            if outcome['status'] == 'inconclusive':
                inconclusive = outcome
        if unconfirmed_region and inconclusive is None:
            return {'status': 'inconclusive', 'reason': 'unexpected_page'}
        return inconclusive or {'status': 'needs_setup', 'reason': 'signed_out'}


def _inspect_verification(name, session, page, targets):
    deadline = monotonic() + 10
    while True:
        if looks_blocked(page):
            return {'status': 'inconclusive', 'reason': 'challenge'}
        if _is_signed_out(name, page.url):
            return {'status': 'needs_setup', 'reason': 'signed_out'}
        current = next((t for t in targets if _same_route(page.url, t.url)), None)
        if current:
            if all(_visible(page, s) for s in current.markers) and _same_route(
                page.url, current.url
            ):
                settings = page.evaluate("""() => ({
                    user_agent: navigator.userAgent, locale: navigator.language,
                    timezone_id: Intl.DateTimeFormat().resolvedOptions().timeZone
                })""")
                settings.update(
                    channel=session.channel,
                    viewport=page.viewport_size,
                    version=getattr(session.context.browser, 'version', None),
                )
                return {
                    'status': 'verified',
                    'state': _scope_captured_state(session.context.storage_state(), name),
                    'capture': settings,
                    'region': current.region,
                    'origin': safe_origin(page.url),
                }
            if any(_visible(page, s) for s in current.signed_out):
                return {'status': 'needs_setup', 'reason': 'signed_out'}
        if monotonic() >= deadline:
            return {
                'status': 'inconclusive',
                'reason': 'marker_missing' if current else 'unexpected_page',
            }
        page.wait_for_timeout(100)


def _check_browser(
    name, state, *, interactive=False, show=False, region=None, capture=None, origin=None
):
    from ..infra.owned_worker import run_worker

    try:
        return run_worker(
            _verification_worker,
            (
                name,
                state,
                interactive,
                {'show': show, 'region': region, 'capture': capture, 'origin': origin},
            ),
            interactive=interactive,
            prompt=Terminal().ask_interruptibly if interactive else None,
            provider=name,
        )
    except CleanupUnconfirmed:
        raise
    except WorkerFailure:
        return {'status': 'inconclusive', 'reason': 'browser_unavailable'}


def _run_verifier(verifier, name, state, *, interactive=False, restore=False, **options):
    if verifier is not verify_state:
        value = verifier(name, state, interactive=True) if interactive else verifier(name, state)
        return (
            {'status': 'verified', 'state': value}
            if value is not None
            else {'status': 'needs_setup'}
        )
    checked = _check_browser(name, state, interactive=interactive, **options)
    if restore and checked['status'] == 'verified':
        captured, _ = validate_state(checked['state'], name)
        checked = _check_browser(
            name,
            captured,
            show=interactive or options.get('show', False),
            region=checked.get('region'),
            capture=checked.get('capture'),
            origin=checked.get('origin'),
        )
    return checked


def _result(name, checked):
    result = {'provider': name, 'status': checked['status']}
    if reason := checked.get('reason'):
        result['reason'] = reason
    if checked['status'] == 'inconclusive':
        result['command'] = f'applicant setup status --provider {name} --check --show'
    return result


def _metadata(checked):
    return {k: checked[k] for k in ('region', 'capture', 'origin') if k in checked} | {
        'checked_at': now().isoformat(),
    }


def provider_session(settings: Settings, name: str):
    try:
        item = provider(name)
    except ValueError as error:
        raise ConfigError(str(error)) from None
    return SessionStore(
        session_path(settings, item.id),
        item.id,
        mode=settings.session_storage,
        dedicated=item.id not in (*PROFILES, 'linkedin'),
    )


def _check_install(installed):
    if installed is None:
        raise ProfileBusy('provider session changed during verification; retry')


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
    db = recovery_database(settings, database)
    store = provider_session(settings, name)
    name = store.provider
    source = Path(source)
    if physical_path(source) == store.path:
        raise ConfigError('import source is the managed session file')
    if source.stat().st_size > 2_000_000:
        raise ConfigError('session file exceeds 2 MB')
    state, warnings = validate_state(read_ranking_document(source), name, format)
    result = {'provider': name, 'status': 'unverified', 'warnings': warnings}
    if dry_run:
        return {**result, 'dry_run': True}
    previous = store.snapshot()
    if previous and previous.get('status') != 'cleared' and not replace:
        raise ConfigError('session already exists; pass --replace')
    if verify:
        checked = _run_verifier(verifier, name, state, restore=True)
        if checked['status'] != 'verified':
            return {**result, **_result(name, checked)}
        state, _ = validate_state(checked['state'], name)
        result['status'] = 'verified'
    _check_install(
        store.install(
            previous,
            state,
            result['status'],
            metadata={
                **(_metadata(checked) if verify else {}),
                **({'pending_database': str(db.resolve())} if db is not None and verify else {}),
            },
            pending='recover' if verify else None,
        )
    )
    return reconcile_session(settings, store, database, result) if verify else result


def setup_provider(
    settings, name, *, reauth=False, verifier=verify_state, database=None, region=None
):
    import sys

    db = recovery_database(settings, database)
    store = provider_session(settings, name)
    name = store.provider
    if region in (None, 'auto'):
        region = None
    elif not any(t.region == region for t in _session_policy(name).targets):
        raise ConfigError(f'{name}: unsupported verification region')
    if not sys.stdin.isatty():
        return {
            'provider': name,
            'status': 'needs_setup',
            'command': f'applicant setup import-session --provider {name} --from-file state.json --verify',
        }
    previous = store.snapshot()
    if previous and previous.get('status') != 'cleared' and not reauth:
        raise ConfigError('session exists; use status --check or provider --reauth')
    checked = _run_verifier(verifier, name, None, interactive=True, restore=True, region=region)
    if checked['status'] != 'verified':
        return _result(name, checked)
    state, warnings = validate_state(checked['state'], name)
    result = {
        'provider': name,
        'status': 'verified',
        'warnings': warnings,
        'checked_at': now().isoformat(),
    }
    _check_install(
        store.install(
            previous,
            state,
            'verified',
            metadata={
                **_metadata(checked),
                **({'pending_database': str(db.resolve())} if db is not None else {}),
            },
            pending='recover',
        )
    )
    return reconcile_session(settings, store, database, result)


def session_status(
    settings, name, *, check=False, verifier=verify_state, database=None, show=False
):
    db = recovery_database(settings, database)
    store = provider_session(settings, name)
    name = store.provider
    previous = store.snapshot()
    if not previous or previous.get('status') == 'cleared':
        result = {'provider': name, 'status': 'needs_setup'}
        return (
            reconcile_session(settings, store, database, result)
            if check and previous.get('pending')
            else result
        )
    status = previous.get('status', 'unverified')
    if previous.get('format') != FORMAT:
        metadata = store.path.with_suffix('.status.json')
        if metadata.exists():
            saved = read_ranking_document(metadata)
            if saved.get('state_hash') == content_hash(previous):
                status = saved.get('status', 'unverified')
    if check:
        if previous.get('pending'):
            return reconcile_session(
                settings,
                store,
                database,
                {'provider': name, 'status': status, 'network_checked': False},
            )
        with store.execution_gate():
            previous, state = store.active_snapshot()
            if state is None or not (state.get('cookies') or state.get('origins')):
                return {'provider': name, 'status': 'needs_setup'}
            state, _ = validate_state(state, name)
            checked = _run_verifier(
                verifier,
                name,
                state,
                region=previous.get('region'),
                capture=previous.get('capture'),
                origin=previous.get('origin'),
                show=show,
            )
            status = checked['status']
            if status == 'inconclusive':
                return {
                    **_result(name, checked),
                    'network_checked': True,
                    'last_verified_at': previous.get('checked_at'),
                }
            stamp = now().isoformat()
            if previous.get('format') != FORMAT:
                rotated = (
                    validate_state(checked['state'], name)[0] if status == 'verified' else state
                )
                saved = store.install(
                    previous,
                    rotated,
                    status,
                    metadata={
                        **(_metadata(checked) if status == 'verified' else {'checked_at': stamp}),
                        **({'pending_database': str(db.resolve())} if db is not None else {}),
                    },
                    pending='recover' if status == 'verified' else 'expire',
                )
            else:
                rotated = {}
                if status == 'verified':
                    rotated['state'], _ = validate_state(checked['state'], name)
                saved = store.update(
                    previous,
                    status=status,
                    pending='recover' if status == 'verified' else 'expire',
                    **(_metadata(checked) if status == 'verified' else {}),
                    **rotated,
                    **({'pending_database': str(db.resolve())} if db is not None else {}),
                )
            _check_install(saved)
    result = {'provider': name, 'status': status, 'network_checked': check}
    return reconcile_session(settings, store, database, result) if check else result


def clear_session(settings, name, *, database=None):
    db = recovery_database(settings, database)
    store = provider_session(settings, name)
    store.clear(database=str(db.resolve()) if db is not None else None)
    return reconcile_session(
        settings,
        store,
        database,
        {
            'provider': store.provider,
            'status': 'cleared',
            'scope': 'local_credentials',
            'legacy_browser_data': 'preserved',
        },
    )
