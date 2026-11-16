"""Explicit SQLite maintenance."""

from ..domain.canonical import canonical_json
from ..infra.store.maintenance import checkpoint
from .database import database_path
from .dispatch import DatabaseRequest, actions, bind


class Checkpoint(DatabaseRequest):
    mode: str = 'passive'


class ResolveExport(DatabaseRequest):
    export: str
    use: str
    confirm: str | None = None


def resolve(args, settings):
    from ..infra.store.export_resolution import resolve_export

    result = resolve_export(
        database_path(settings, args.database), settings.path(args.export), args.use, args.confirm
    )
    print(canonical_json(result))
    return 0


def add_parser(add):
    group = actions(add('store', help='maintain an existing SQLite store'))
    p = group('checkpoint', help='checkpoint WAL without creating or migrating a database')
    p.add_argument('--database')
    p.add_argument('--mode', choices=('passive', 'truncate'), default='passive')
    p.add_argument('--json', action='store_true')
    bind(p, Checkpoint, run)
    p = group('resolve-export', help='preview or resolve a managed export conflict')
    p.add_argument('--database')
    p.add_argument('--export', required=True)
    p.add_argument('--use', choices=('sqlite', 'file'), required=True)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--confirm', metavar='TOKEN')
    bind(p, ResolveExport, resolve)


def run(args, settings):
    result = checkpoint(database_path(settings, args.database), args.mode)
    print(canonical_json(result))
    return int(args.mode == 'truncate' and result['status'] in ('busy', 'partial'))
