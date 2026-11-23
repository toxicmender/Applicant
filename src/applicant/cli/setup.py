"""Explicit authentication and offline/headless session import."""

from ..domain.canonical import canonical_json
from ..errors import ConfigError
from ..services.provider_setup import clear_session, import_session, session_status, setup_provider
from ..settings import Settings
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
    show: bool = False
    region: str | None = None


def add_parser(add):
    group = actions(add('setup', help='authenticate providers or import saved sessions'))
    for action in ('provider', 'status', 'import-session', 'clear'):
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
            p.add_argument('--region')
        elif action == 'status':
            p.add_argument('--check', action='store_true')
            p.add_argument('--show', action='store_true')
        elif action == 'import-session':
            p.add_argument('--from-file', required=True)
            p.add_argument('--format', choices=('playwright', 'cookies'), default='playwright')
            for flag in ('dry-run', 'replace', 'verify'):
                p.add_argument('--' + flag, action='store_true')
        bind(p, Setup, run)


def run(args, settings):
    if args.database and settings.store == 'files':
        raise ConfigError('--database cannot be combined with --store files')
    if args.operation == 'provider':
        result = setup_provider(
            settings, args.provider, reauth=args.reauth, database=args.database, region=args.region
        )
    elif args.operation == 'status':
        if args.show and not args.check:
            raise ConfigError('--show requires --check')
        result = session_status(
            settings, args.provider, check=args.check, database=args.database, show=args.show
        )
    elif args.operation == 'clear':
        result = clear_session(settings, args.provider, database=args.database)
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
    return output_result(result, args.database)


def output_result(result, database=None):
    code = 0
    recovery = result.get('health_recovery', {})
    if recovery.get('status') in (
        'database_busy',
        'database_error',
        'schema_incompatible',
        'leased',
    ):
        action = 'clear' if result['status'] == 'cleared' else 'status'
        result['command'] = f'applicant setup {action} --provider {result["provider"]}'
        if action == 'status':
            result['command'] += ' --check'
        if database:
            result['command'] += f' --database "{database}"'
        code = 8 if recovery.get('retryable') else 1
    elif result['status'] == 'needs_setup':
        code = 3
    elif result['status'] == 'inconclusive':
        code = {'challenge': 11, 'browser_unavailable': 7}.get(result.get('reason'), 5)
    print(canonical_json(result))
    return code


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
    return output_result(result)
