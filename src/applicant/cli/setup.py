"""Explicit authentication and offline/headless session import."""

from ..domain.canonical import canonical_json
from ..services.provider_setup import import_session, session_status, setup_provider
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
    if args.operation == 'provider':
        result = setup_provider(settings, args.provider, reauth=args.reauth)
    elif args.operation == 'status':
        result = session_status(settings, args.provider, check=args.check)
    else:
        result = import_session(
            settings,
            args.provider,
            settings.path(args.from_file),
            format=args.format,
            dry_run=args.dry_run,
            replace=args.replace,
            verify=args.verify,
        )
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
    print(canonical_json(result))
    return 3 if result['status'] == 'needs_setup' else 0
