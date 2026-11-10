"""Inspect provider capabilities without starting browsers or network requests."""

from ..domain.canonical import canonical_json
from ..domain.providers import PROVIDERS, provider
from ..services.provider_health import health
from ..services.research_feed import collect_feed
from .database import database_path
from .dispatch import DatabaseRequest, actions, bind


class Sources(DatabaseRequest):
    provider: str | None = None
    operation: str = 'list'


def add_parser(add):
    group = actions(add('sources', help='inspect provider capabilities'))
    for name in ('list', 'show', 'health', 'probe'):
        parser = group(name, help=f'{name} provider capabilities')
        if name == 'show':
            parser.add_argument('provider')
        elif name in ('health', 'probe'):
            parser.add_argument('--provider', required=True)
            parser.add_argument('--database')
        parser.set_defaults(operation=name)
        parser.add_argument('--json', action='store_true')
        bind(parser, Sources, run)


def run(args, settings):
    if args.operation in ('health', 'probe'):
        name = provider(args.provider).id
        database = database_path(settings, args.database)
        if args.operation == 'health':
            result = (
                health(database, name) if database.exists() else {'status': 'ready', 'failures': 0}
            )
            print(canonical_json(result))
            return 0
        else:
            collection = collect_feed(
                database, {'providers': [name], 'limit': 1, 'keywords': ''}, probe=True
            )
        print(canonical_json(collection))
        return int(not collection['job_version_ids'])
    items = [provider(args.provider)] if args.provider else list(PROVIDERS.values())
    print(canonical_json({'providers': [p.document() for p in items]}))
    return 0
