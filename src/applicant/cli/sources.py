"""Inspect provider operations and run explicit, bounded probes."""

from typing import get_args

from ..domain.canonical import canonical_json
from ..domain.providers import PROVIDERS, provider
from ..domain.source_operations import OperationName, eligible_route
from ..errors import ConfigError, DatabaseBusy
from ..infra.owned_worker import CleanupUnconfirmed
from ..services.provider_health import health
from ..services.provider_setup import provider_session, session_status
from ..services.research_feed import collect_feed
from ..services.source_operations import operations, project
from .database import database_path
from .dispatch import DatabaseRequest, actions, bind


class Sources(DatabaseRequest):
    provider: str | None = None
    operation: str = 'list'
    probe_operation: OperationName | None = None
    route: str | None = None
    input: str | None = None


_EXIT = {
    'success': 0,
    'empty': 0,
    'verified': 0,
    'cleanup_unconfirmed': 1,
    'infrastructure_failure': 1,
    'route_unavailable': 2,
    'approval_required': 2,
    'unreviewed': 2,
    'prohibited': 2,
    'destination_denied': 2,
    'input_required': 2,
    'needs_setup': 3,
    'blocked': 3,
    'throttled': 3,
    'cooldown': 3,
    'missing': 3,
    'expired': 3,
    'adapter_degraded': 5,
    'invalid': 6,
    'insufficient_scope': 6,
    'unavailable': 7,
    'browser_unavailable': 7,
    'browser_teardown_timeout': 7,
    'leased': 8,
    'database_busy': 8,
    'challenge': 11,
}


def add_parser(add):
    group = actions(add('sources', help='inspect provider capabilities'))
    for name in ('list', 'show', 'health', 'probe'):
        parser = group(name, help=f'{name} provider capabilities')
        if name == 'show':
            parser.add_argument('provider')
        elif name in ('health', 'probe'):
            parser.add_argument('--provider', required=True)
        parser.add_argument('--database')
        if name == 'probe':
            parser.add_argument(
                '--operation', dest='probe_operation', choices=get_args(OperationName)
            )
            parser.add_argument('--route')
            parser.add_argument('--input')
            parser.epilog = (
                'Typed exits: 0 completed, 1 operation failed, 2 invalid or unavailable route, '
                '3 setup required, blocked or throttled, 5 parser drift, 6 API authorization or '
                'quota, 7 upstream unavailable, 8 busy, 11 challenge, 130 interrupted. '
                'Legacy probes use 0 for jobs returned and 1 otherwise.'
            )
        parser.set_defaults(operation=name)
        parser.add_argument('--json', action='store_true')
        bind(parser, Sources, run)


def run(args, settings):
    database = database_path(settings, args.database) if settings.store == 'sqlite' else None
    if args.database and database is None:
        raise ConfigError('--database requires --store sqlite')
    has_database = database is not None and database.exists()
    if args.operation == 'health':
        name = provider(args.provider).id
        print(
            canonical_json(
                health(database, name) if has_database else {'status': 'ready', 'failures': 0}
            )
        )
        return 0
    if args.operation == 'probe':
        item = provider(args.provider)
        return _probe(
            args,
            settings,
            database,
            item,
            _project(item, settings, database if has_database else None),
        )
    items = [provider(args.provider)] if args.provider else list(PROVIDERS.values())
    print(
        canonical_json(
            {
                'operations_schema_version': 1,
                'providers': [
                    {
                        **p.document(),
                        'operations': {
                            key: value.model_dump(mode='json')
                            for key, value in _project(
                                p, settings, database if has_database else None
                            ).items()
                        },
                    }
                    for p in items
                ],
            }
        )
    )
    return 0


def _project(item, settings, database):
    state = health(database, item.id) if database is not None else {}
    credential = 'unknown'
    if item.session:
        store = provider_session(settings, item.id)
        credential = 'missing'
        if store.path.is_file():
            # Offline metadata never calls the OS keyring or launches a browser.
            recorded = session_status(settings, item.id)['status']
            credential = {
                'verified': 'valid',
                'needs_setup': 'expired',
                'cleared': 'missing',
            }.get(recorded, 'present_unverified')
    return {
        key: project(operation, state, credential) for key, operation in operations(item).items()
    }


def _probe(args, settings, database, item, declared):
    if (args.route or args.input) and not args.probe_operation:
        raise ConfigError('--route and --input require --operation')
    key = args.probe_operation or 'job_search'
    operation = declared.get(key)
    if args.route and (not operation or args.route not in {route.id for route in operation.routes}):
        raise ConfigError('unknown route for the selected provider and operation')
    route, exclusions = None, {}
    if operation:
        route, exclusions = eligible_route(
            operation,
            'persist' if key == 'job_search' and not args.input else 'display',
            pin=args.route,
            has_input=bool(args.input),
        )
    selected = route.id if route else next(iter(exclusions), None)
    eligible = None
    outcome = {'status': 'route_unavailable'}
    collection = {'job_version_ids': [], 'observations': [], 'outcomes': [], 'groups': []}
    try:
        if route is None:
            reason = next(iter(exclusions.values()), 'route_unavailable')
            outcome = {'status': reason}
            if operation:
                blocked = next((x for x in operation.routes if x.id == selected), None)
                if blocked is not None and blocked.runtime.eligible_at is not None:
                    eligible = blocked.runtime.eligible_at.isoformat()
        elif route.method == 'user_import':
            outcome = {'status': 'input_required'}
        elif key == 'job_search' and database is not None:
            collection = collect_feed(
                database,
                {'providers': [item.id], 'limit': 1, 'keywords': ''},
                probe=True,
                settings=settings,
                use_session=route.credential_kind == 'browser_session',
            )
            outcome = collection['outcomes'][0]
            if outcome['status'] == 'success' and not collection['job_version_ids']:
                outcome = {**outcome, 'status': 'empty'}
            eligible = outcome.get('eligible_at')
        elif key == 'account_setup':
            outcome = session_status(settings, item.id, check=True, database=args.database)
    except CleanupUnconfirmed:
        outcome = {'status': 'cleanup_unconfirmed'}
    except DatabaseBusy:
        outcome = {'status': 'database_busy'}
    status = outcome['status']
    reason = outcome.get('reason') or (
        status if status not in ('success', 'verified', 'empty') else None
    )
    code = _EXIT.get(reason or status, _EXIT.get(status, 5))
    print(
        canonical_json(
            {
                **collection,
                **outcome,
                'operation': key,
                'route': selected,
                'reason': reason,
                'eligible_at': eligible,
                'operation_exit_code': code,
                'exclusions': exclusions,
            }
        )
    )
    if args.probe_operation:
        return code
    return int(code != 0 or not collection['job_version_ids'])
