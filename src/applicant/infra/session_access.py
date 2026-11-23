"""Session-aware operations keep network work outside the credential lock."""

from contextlib import contextmanager
from contextvars import ContextVar
from http.cookiejar import Cookie, DefaultCookiePolicy
from time import time
from urllib.parse import urlsplit

from httpx import Cookies

from ..errors import AuthFailed, ProfileBusy

active_access: ContextVar['SessionAccess | None'] = ContextVar(
    'active_session_access', default=None
)
_CHALLENGE_COOKIES = {'cf_clearance', '__cf_bm', '_abck', 'ak_bmsc', 'bm_sz'}


class SessionExpired(AuthFailed):
    """A provider visibly rejected saved credentials."""


def seed_cookie_jar(jar, state, *, clock=time):
    jar.clear()
    jar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    cutoff = clock() + 30
    for cookie in state['cookies']:
        expiry = cookie.get('expires', -1)
        if expiry != -1 and expiry <= cutoff:
            continue
        domain = cookie['domain']
        domain_cookie = domain.startswith('.')
        path = cookie['path']
        rest = {'HttpOnly': ''} if cookie.get('httpOnly') else {}
        jar.set_cookie(
            Cookie(
                0,
                cookie['name'],
                cookie['value'],
                None,
                False,
                domain,
                domain_cookie,
                domain_cookie,
                path,
                True,
                cookie.get('secure', True),
                None if expiry == -1 else int(expiry),
                expiry == -1,
                None,
                None,
                rest,
            )
        )


def apply_cookie_scope(request, jar):
    # HTTPX merges request cookies into a fresh jar with its default policy.
    # Rebuild the header from the strict jar on every request, including redirects.
    request.headers.pop('cookie', None)
    Cookies(jar).set_cookie_header(request)


def cookie_state(jar, state, *, clock=time):
    cutoff = clock() + 30
    previous = {(c['name'], c['domain'], c['path']): c for c in state['cookies']}
    cookies = []
    for cookie in jar:
        domain = cookie.domain if cookie.domain_specified else cookie.domain.lstrip('.')
        if cookie.domain_specified and not domain.startswith('.'):
            domain = '.' + domain
        key = cookie.name, domain, cookie.path
        old = previous.get(key, {})
        if cookie.expires is not None and cookie.expires <= cutoff:
            continue
        cookies.append(
            {
                **old,
                'domain': domain,
                'name': cookie.name,
                'path': cookie.path,
                'value': cookie.value,
                'expires': -1 if cookie.expires is None else cookie.expires,
                'secure': cookie.secure,
                'httpOnly': any(k.lower() == 'httponly' for k in cookie._rest),
            }
        )
    return {'cookies': cookies, 'origins': state['origins']}


class SessionAccess:
    def __init__(self, store, record, session_state):
        self.store = store
        self.record = record
        self.state = session_state

    @contextmanager
    def exchange(self):
        with self.store.execution_gate():
            current, state = self.store.active_snapshot()
            if (
                current.get('generation') != self.record.get('generation')
                or current.get('status') == 'needs_setup'
                or state is None
            ):
                raise ProfileBusy('provider session changed; retry the operation')
            from ..services.provider_setup import validate_state

            self.record = current
            self.state, _ = validate_state(state, self.store.provider)
            yield

    def publish(self, state):
        from ..services.provider_setup import _scope_captured_state, validate_state

        scoped = _scope_captured_state(state, self.store.provider)
        scoped['cookies'] = [c for c in scoped['cookies'] if c['name'] not in _CHALLENGE_COOKIES]
        previous = self.state
        if scoped == previous or not (scoped['cookies'] or scoped['origins']):
            return
        normalized, _ = validate_state(scoped, self.store.provider)
        updated = self.store.update(self.record, state=normalized)
        if updated is None:
            raise ProfileBusy('provider session changed before credential rotation was saved')
        self.record, self.state = updated, normalized

    def check_page(self, page):
        from ..services.provider_setup import _is_signed_out

        if _is_signed_out(self.store.provider, page.url):
            raise SessionExpired(f'{self.store.provider}: account session expired')

    def http(self, client, request, *, accepted, confirms_expiry=False):
        from ..services.provider_setup import _allowed_domain, _is_signed_out, _provider_domains

        with self.exchange():
            jar = client.cookies.jar
            seed_cookie_jar(jar, self.state)
            if locale := (self.record.get('capture') or {}).get('locale'):
                client.headers['Accept-Language'] = locale

            def scope(request):
                apply_cookie_scope(request, jar)

            hooks = client.event_hooks['request']
            hooks.append(scope)
            try:
                response = request()
                host = urlsplit(str(response.url)).hostname
                if confirms_expiry and _is_signed_out(self.store.provider, str(response.url)):
                    raise SessionExpired(f'{self.store.provider}: account session expired')
                if accepted(response) and _allowed_domain(
                    host, _provider_domains(self.store.provider)
                ):
                    self.publish(cookie_state(jar, self.state))
                return response
            finally:
                hooks.remove(scope)
                jar.clear()


def run_with_session(store, operation, *, public):
    """Retry the original operation once with a fresh anonymous adapter on expiry."""
    if not store.path.is_file():
        return operation(), {'access_mode': 'public'}
    record, state = store.active_snapshot()
    if state is None or record.get('status') == 'needs_setup':
        return operation(), {'access_mode': 'public'}
    from ..services.provider_setup import validate_state

    state, _ = validate_state(state, store.provider)
    access = SessionAccess(store, record, state)
    token = active_access.set(access)
    try:
        return operation(), {'access_mode': 'authenticated'}
    except SessionExpired:
        updated = store.update(access.record, status='needs_setup', pending='expire')
        if updated is None:
            raise ProfileBusy('provider session changed during expiry detection') from None
        if not public:
            raise
    finally:
        active_access.reset(token)
    return operation(), {
        'access_mode': 'public',
        'reason': 'session_expired',
        'command': f'applicant setup provider {store.provider} --reauth',
    }
