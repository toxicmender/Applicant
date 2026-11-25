"""Inspect provider operations and run explicit, bounded probes."""

from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from ..domain.canonical import canonical_json
from ..domain.providers import PROFILE_PROVIDERS, PROVIDERS, provider
from ..domain.source_operations import OperationName, eligible_route
from ..errors import ConfigError, DatabaseBusy
from ..infra.owned_worker import CleanupUnconfirmed
from ..infra.ranking_documents import read_ranking_document
from ..services.company_capture import capture_profile
from ..services.provider_health import health, route_health
from ..services.provider_setup import provider_session, session_status
from ..services.research_feed import collect_feed
from ..services.source_import import sample_import, validate_source_import
from ..services.source_operations import operations, project
from .database import database_path
from .dispatch import DatabaseRequest, actions, bind


class Sources(DatabaseRequest):
    provider: str | None = None
    operation: str = 'list'
    probe_operation: OperationName | None = None
    route: str | None = None
    input: str | None = None

    @property
    def import_probe(self) -> bool:
        if self.operation != 'probe' or not self.provider or not self.probe_operation:
            return False
        if self.route is not None:
            return self.route == 'import'
        selected = operations(provider(self.provider)).get(self.probe_operation)
        return bool(
            self.input and selected and any(r.method == 'user_import' for r in selected.routes)
        )


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
    'invalid_input': 2,
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
    if args.import_probe:
        item = provider(args.provider)
        return _probe(args, settings, None, item, operations(item))
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
            _project(item, settings, database if has_database else None, probe=True),
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


def _project(item, settings, database, *, probe=False):
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
    result = {}
    for key, operation in operations(item).items():
        projected = project(operation, state, credential)
        if database is not None:
            routes = []
            for route in projected.routes:
                recorded = route_health(database, item.id, key, route.id)
                if (
                    recorded
                    and route.runtime.health not in ('cooldown', 'leased')
                    and (not probe or recorded['adapter_revision'] == route.adapter_revision)
                ):
                    route = route.model_copy(
                        update={
                            'runtime': route.runtime.model_copy(
                                update={
                                    'health': 'adapter_degraded',
                                    'reason': 'parser_quarantined',
                                },
                            )
                        }
                    )
                routes.append(route)
            projected = projected.model_copy(update={'routes': tuple(routes)})
        result[key] = projected
    return result


def _probe(args, settings, database, item, declared):
    key = args.probe_operation or 'job_search'
    operation = declared.get(key)
    invocation_error = None
    if (args.route or args.input) and not args.probe_operation:
        invocation_error = '--route and --input require --operation'
    elif args.route and (
        not operation or args.route not in {route.id for route in operation.routes}
    ):
        invocation_error = 'unknown route for the selected provider and operation'
    route, exclusions = None, {}
    if operation and invocation_error is None:
        implicit_import = args.input and any(r.method == 'user_import' for r in operation.routes)
        pin = args.route or ('import' if implicit_import else None)
        route, exclusions = eligible_route(
            operation,
            'persist'
            if key in ('job_search', 'job_detail', 'company_profile') and pin != 'import'
            else 'display',
            pin=pin,
            has_input=bool(args.input),
        )
    selected = route.id if route else next(iter(exclusions), None)
    eligible = None
    outcome = {'status': 'route_unavailable'}
    collection = {'job_version_ids': [], 'observations': [], 'outcomes': [], 'groups': []}
    try:
        if invocation_error is not None:
            outcome = {'status': 'invalid_input', 'message': invocation_error}
        elif route is None:
            reason = next(iter(exclusions.values()), 'route_unavailable')
            outcome = {'status': reason}
            if operation:
                blocked = next((x for x in operation.routes if x.id == selected), None)
                if blocked is not None and blocked.runtime.eligible_at is not None:
                    eligible = blocked.runtime.eligible_at.isoformat()
                    if reason == 'cooldown' and blocked.runtime.reason is not None:
                        outcome['reason'] = blocked.runtime.reason
        elif route.method == 'user_import':
            fixture = not args.input
            value = (
                sample_import(item.id, key)
                if fixture
                else read_ranking_document(Path(settings.path(args.input)))
            )
            validate_source_import(
                value, expected=(item.id, key), destination='display', fixture=fixture
            )
            outcome = {
                'status': 'success',
                'validation_kind': 'fixture' if fixture else 'user_input',
            }
        elif key in ('job_search', 'job_detail') and database is not None:
            query = {'providers': [item.id], 'limit': 1, 'keywords': ''}
            if key == 'job_detail':
                if not args.input:
                    raise ConfigError('job_detail requires --input with a JSON url field')
                source = read_ranking_document(Path(settings.path(args.input)))
                if (
                    not isinstance(source, dict)
                    or set(source) != {'url'}
                    or not isinstance(source['url'], str)
                ):
                    raise ConfigError('job_detail requires a JSON object containing a url')
                query['urls'] = {item.id: [source['url']]}
            collection = collect_feed(
                database,
                query,
                probe=True,
                settings=settings,
                use_session=route.credential_kind == 'browser_session',
                selected_route=(key, route.id, route.adapter_revision),
            )
            outcome = collection['outcomes'][0]
            if outcome['status'] == 'success' and not collection['job_version_ids']:
                outcome = {**outcome, 'status': 'empty'}
            eligible = outcome.get('eligible_at')
        elif key == 'company_profile' and item.id in PROFILE_PROVIDERS and database is not None:
            if not args.input:
                raise ConfigError('company_profile requires a verified company selection file')
            selection = read_ranking_document(Path(settings.path(args.input)))
            if not isinstance(selection, dict) or selection.get('provider') not in (
                item.id,
                *item.aliases,
            ):
                raise ConfigError('company selection does not match the selected provider')
            outcome = capture_profile(database, selection)
            if 'evidence_id' in outcome:
                outcome = {**outcome, 'status': 'success'}
        elif key == 'account_setup':
            outcome = session_status(settings, item.id, check=True, database=args.database)
            recovery = outcome.get('health_recovery')
            if isinstance(recovery, dict):
                if recovery.get('retryable'):
                    outcome['reason'] = 'database_busy'
                elif recovery.get('status') in ('database_error', 'schema_incompatible'):
                    outcome['reason'] = 'infrastructure_failure'
    except CleanupUnconfirmed:
        outcome = {'status': 'cleanup_unconfirmed'}
    except DatabaseBusy:
        outcome = {'status': 'database_busy'}
    except (ConfigError, ValidationError):
        outcome = {'status': 'invalid_input'}
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
                'access_mode': outcome.get('access_mode')
                or ('public' if route and route.method.startswith('public_') else None),
            }
        )
    )
    if args.probe_operation or invocation_error is not None:
        return code
    return int(code != 0 or not collection['job_version_ids'])
