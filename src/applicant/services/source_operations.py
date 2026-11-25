"""Resolve the catalog against offline provider health and account metadata."""

from datetime import datetime

from ..domain.contracts import now
from ..domain.providers import PROFILE_PROVIDERS, RESTRICTED_PUBLIC_ROUTES, Provider
from ..domain.source_operations import Operation, OperationName, Route, Runtime, Validation

_PUBLIC_CONTROLS = ('weworkremotely', 'wellfound', 'builtin')
_PUBLIC_FAILURES = {
    'welcometothejungle': ('failed', 'unparseable'),
    'arc': ('failed', 'unparseable'),
    'instahyre': ('blocked', 'access_blocked'),
}
_SETUP_PASSES = {
    'indeed': 25,
    'naukri': 24,
    'googlejobs': 24,
    'glassdoor': 25,
    'ambitionbox': 25,
    'levels': 24,
}


def _historical_visit(day, outcome='passed', reason=None):
    stamp = datetime.fromisoformat(f'2026-11-{day:02}T00:00:00+00:00')
    return Validation(
        outcome=outcome,
        checked_at=stamp,
        reason=reason,
        last_passed_at=stamp if outcome == 'passed' else None,
    )


def _route(id, method, *, priority=10, established=True, credential='none', **options):
    destinations = options.pop(
        'destinations', ('display', 'persist', 'export') if established else ()
    )
    return Route(
        id=id,
        priority=priority,
        method=method,
        credential_kind=credential,
        policy=options.pop('policy', 'allowed' if established else 'unreviewed'),
        authority=options.pop(
            'authority', 'existing Applicant operation' if established else 'pending access review'
        ),
        destinations=destinations,
        retention='durable' if set(destinations) - {'display'} else 'none',
        adapter_revision='1',
        **options,
    )


def operations(item: Provider) -> dict[OperationName, Operation]:
    name = item.id
    result: dict[OperationName, Operation] = {}

    def add(key: OperationName, route: Route, *additional: Route, implementation='implemented'):
        result[key] = Operation(implementation=implementation, routes=(route, *additional))

    def imported(key: OperationName, *routes: Route):
        add(
            key, _route('import', 'user_import', priority=90), *routes, implementation='import_only'
        )

    if 'jobs' in item.domains:
        if item.board:
            method = (
                'public_feed'
                if item.access == 'public_feed'
                else 'public_structured'
                if item.access == 'public_structured_or_import'
                else 'public_browser'
            )
            outcome = _PUBLIC_FAILURES.get(name)
            public = _route(
                'public',
                method,
                priority=20,
                fixture=Validation(outcome='passed')
                if name in (*_PUBLIC_CONTROLS, *_PUBLIC_FAILURES)
                else Validation(),
                live=_historical_visit(10)
                if name in _PUBLIC_CONTROLS
                else _historical_visit(10, *outcome)
                if outcome
                else Validation(),
            )
            if name in RESTRICTED_PUBLIC_ROUTES:
                public = public.model_copy(
                    update={
                        'policy': 'prohibited',
                        'destinations': (),
                        'retention': 'none',
                        'authority': RESTRICTED_PUBLIC_ROUTES[name],
                        'reviewed_at': datetime.fromisoformat('2026-11-25T00:00:00+00:00'),
                    }
                )
                imported('job_search', public)
            elif name in ('indeed', 'naukri', 'googlejobs'):
                add(
                    'job_search',
                    _route(
                        'session',
                        'managed_browser',
                        credential='browser_session',
                        fallback='public',
                    ),
                    public,
                )
            else:
                add('job_search', public)
            if method == 'public_structured':
                detail = public.model_copy(update={'live': Validation()})
                if name in RESTRICTED_PUBLIC_ROUTES:
                    imported('job_detail', detail)
                else:
                    add('job_detail', detail)
        else:
            method = {'ziprecruiter': 'official_mcp', 'upwork': 'official_api'}.get(
                name, 'public_structured'
            )
            imported(
                'job_search',
                _route(
                    'live',
                    method,
                    established=False,
                    credential='oauth2' if name == 'upwork' else 'none',
                    policy='approval_required' if name == 'upwork' else 'unreviewed',
                ),
            )
    if item.session:
        add(
            'account_setup',
            _route(
                'session',
                'managed_browser',
                credential='browser_session',
                destinations=('display',),
                live=_historical_visit(_SETUP_PASSES[name])
                if name in _SETUP_PASSES
                else Validation(),
                policy='approval_required' if name == 'levels' else 'allowed',
            ),
        )
    elif name == 'weworkremotely':
        add(
            'account_setup',
            _route('session', 'managed_browser', established=False, credential='browser_session'),
            implementation='pending',
        )
    if name in RESTRICTED_PUBLIC_ROUTES and 'company' in item.domains:
        imported(
            'company_profile',
            _route(
                'public',
                'public_structured',
                established=False,
                policy='prohibited',
                authority=RESTRICTED_PUBLIC_ROUTES[name],
            ),
        )
    elif name in PROFILE_PROVIDERS:
        add('company_profile', _route('public', 'public_structured'))
    elif name in ('crunchbase', 'tracxn'):
        add('company_profile', _route('session', 'managed_browser', credential='browser_session'))
    elif 'company' in item.domains:
        imported('company_profile')
    if 'culture' in item.domains:
        if name in ('glassdoor', 'ambitionbox'):
            add(
                'workplace_reviews',
                _route(
                    'session',
                    'managed_browser',
                    credential='browser_session',
                    destinations=('display',),
                    fallback='public',
                ),
                _route('public', 'public_browser', priority=20),
            )
        else:
            imported('workplace_reviews')
    if 'workplace' in item.domains:
        imported('workplace_policy')
    if 'compensation' in item.domains:
        routes = (
            (
                _route(
                    'api',
                    'official_api',
                    established=False,
                    credential='api_key',
                    policy='approval_required',
                    authority='https://www.levels.fyi/about/terms',
                ),
            )
            if name == 'levels'
            else ()
        )
        imported('compensation', *routes)
    return result


def project(operation, health, credential='unknown', *, clock=now):
    current = clock()
    until = health.get('eligible_at')
    lease = health.get('lease_until')
    status = health.get('status', 'unknown')
    if lease and datetime.fromisoformat(lease) > current:
        status = 'leased'
    elif status == 'cooldown' and until and datetime.fromisoformat(until) <= current:
        status = 'ready'
    states = []
    for route in operation.routes:
        state = 'not_required' if route.credential_kind == 'none' else credential
        states.append(
            route.model_copy(
                update={
                    'runtime': Runtime(
                        health=status if route.method != 'user_import' else 'unknown',
                        reason=health.get('reason') if route.method != 'user_import' else None,
                        eligible_at=until if route.method != 'user_import' else None,
                        lease_until=lease if route.method != 'user_import' else None,
                        credential_status=state,
                    )
                }
            )
        )
    return operation.model_copy(update={'routes': tuple(states)})
