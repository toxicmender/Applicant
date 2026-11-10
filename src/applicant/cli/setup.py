"""Explicit authentication and offline/headless session import."""

from ..domain.canonical import canonical_json
from ..errors import ConfigError
from ..services.provider_health import recover_authentication
from ..services.provider_setup import import_session, session_status, setup_provider
from ..settings import Settings
from .database import database_path
from .dispatch import DatabaseRequest, actions, bind


class Setup(DatabaseRequest):
    provider: str
    operation: str
    from_file: str | None = None
    format: str = 'playwright'
    dry_run: bool = False
    replace: bool = False
    verify: bool = False
    reauth: bool = False
    check: bool = False


def add_parser(add):
    group = actions(add('setup', help='authenticate providers or import saved sessions'))
    for action in ('provider', 'status', 'import-session'):
        p = group(action, help=action + ' provider authentication')
        p.set_defaults(operation=action)
        if action == 'provider':
            p.add_argument('provider')
        else:
            p.add_argument('--provider', required=True)
        p.add_argument('--json', action='store_true')
        p.add_argument('--database')
        if action == 'provider':
            p.add_argument('--reauth', action='store_true')
        elif action == 'status':
            p.add_argument('--check', action='store_true')
        else:
            p.add_argument('--from-file', required=True)
            p.add_argument('--format', choices=('playwright', 'cookies'), default='playwright')
            for flag in ('dry-run', 'replace', 'verify'):
                p.add_argument('--' + flag, action='store_true')
        bind(p, Setup, run)


def run(args, settings):
    if args.database and settings.store == 'files':
        raise ConfigError('--database cannot be combined with --store files')
    if args.operation == 'provider':
        result = setup_provider(settings, args.provider, reauth=args.reauth, database=args.database)
    elif args.operation == 'status':
        result = session_status(settings, args.provider, check=args.check, database=args.database)
    else:
        result = import_session(
            settings,
            args.provider,
            settings.path(args.from_file),
            format=args.format,
            dry_run=args.dry_run,
            replace=args.replace,
            verify=args.verify,
            database=args.database,
        )
    return output_result(
        result,
        settings,
        args.database,
        recover=args.operation == 'provider' or args.check or args.verify,
    )


def output_result(result, settings, database=None, *, recover=True):
    if recover and result['status'] == 'verified':
        result['health_recovery'] = result.get('health_recovery') or (
            {'status': 'skipped_file_only'}
            if settings.store == 'files'
            else recover_authentication(database_path(settings, database), result['provider'])
        )
        if result['health_recovery']['status'] in (
            'database_busy',
            'database_error',
            'schema_incompatible',
        ):
            result['command'] = f'applicant setup status --provider {result["provider"]} --check'
            if database:
                result['command'] += f' --database "{database}"'
            print(canonical_json(result))
            return 8 if result['health_recovery'].get('retryable') else 1
    print(canonical_json(result))
    return 3 if result['status'] == 'needs_setup' else 0


def legacy_login(
    settings: Settings,
    name: str,
    *,
    profile: str | None = None,
    cookies: str | None = None,
    reauth: bool = True,
) -> int:
    """Existing login flags use the same explicit setup and private state paths."""
    fields = {}
    if profile:
        fields[
            {
                'glassdoor': 'glassdoor_profile',
                'crunchbase': 'crunchbase_profile',
                'tracxn': 'tracxn_profile',
            }[name]
        ] = profile
    if cookies:
        fields['cookies'] = cookies
    configured = settings.model_copy(update={'files': settings.files.model_copy(update=fields)})
    result = setup_provider(configured, name, reauth=reauth)
    return output_result(result, configured)
