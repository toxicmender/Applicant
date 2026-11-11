"""Explicit SQLite maintenance."""

from ..domain.canonical import canonical_json
from ..infra.store.maintenance import checkpoint
from .database import database_path
from .dispatch import DatabaseRequest, actions, bind


class Checkpoint(DatabaseRequest):
    mode: str = 'passive'


def add_parser(add):
    group = actions(add('store', help='maintain an existing SQLite store'))
    p = group('checkpoint', help='checkpoint WAL without creating or migrating a database')
    p.add_argument('--database')
    p.add_argument('--mode', choices=('passive', 'truncate'), default='passive')
    p.add_argument('--json', action='store_true')
    bind(p, Checkpoint, run)


def run(args, settings):
    result = checkpoint(database_path(settings, args.database), args.mode)
    print(canonical_json(result))
    return int(args.mode == 'truncate' and result['status'] in ('busy', 'partial'))
