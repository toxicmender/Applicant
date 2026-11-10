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


def session_path(settings, name):
    item = provider(name)
    if not item.login_url:
        raise ConfigError(f'{item.id}: session setup is not supported')
    if item.id == 'linkedin':
        return Path(settings.path(settings.files.cookies))
    return Path(settings.path(getattr(settings.files, PROFILES[item.id]))) / 'storage_state.json'


def validate_state(value, name, format='playwright'):
    root = (urlsplit(provider(name).url).hostname or '').removeprefix('www.')

    def allowed(host):
        return isinstance(host, str) and (
            host.lstrip('.') == root or host.lstrip('.').endswith('.' + root)
        )

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
            or not allowed(cookie['domain'])
            or not cookie['path'].startswith('/')
        ):
            raise ConfigError(
                'cookie has invalid fields or a domain outside the provider allowlist'
            )
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
            or not allowed(url.hostname)
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
    item = provider(name)
    if not item.login_url or not item.check_url or not item.auth_selector:
        raise ConfigError('provider has no authentication adapter')
    with factory(headless=not interactive, storage_state=state) as session:
        page = session.start()
        page.goto(item.login_url if interactive else item.check_url, wait_until='domcontentloaded')
        if interactive:
            Terminal().pause('Complete login/MFA in the browser, then press Enter to verify: ')
            page.goto(item.check_url, wait_until='domcontentloaded')
        if looks_blocked(page) or any(
            part in page.url.lower() for part in ('/login', '/authwall', '/checkpoint', '/signin')
        ):
            return None
        if not page.locator(item.auth_selector).count():
            return None
        return session.context.storage_state()


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
):
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
    return result


def setup_provider(settings, name, *, reauth=False, verifier=verify_state):
    import sys

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
        return result


def session_status(settings, name, *, check=False, verifier=verify_state):
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
        return {'provider': name, 'status': status, 'network_checked': check}
