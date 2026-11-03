"""Explicit generation, inspection and acceptance of weight suggestions."""

import sys

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..errors import ConfigError
from ..infra.store.personalization import SuggestionNotFound
from ..infra.store.profiles import RevisionConflict
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.personalization import accept_suggestion, show_suggestion, suggest_weights
from .feedback import database_path


def add_parser(add):
    command = add('personalize', help='preview and explicitly accept bounded weight suggestions')
    command.add_argument('action', choices=('suggest', 'show', 'accept'))
    command.add_argument('--database')
    command.add_argument('--run')
    command.add_argument('--suggestion')
    command.add_argument('--json', action='store_true')
    return command


def run(args) -> int:
    try:
        database = database_path(args)
        if args.action == 'suggest':
            if not args.run or not args.run.strip() or args.suggestion is not None:
                raise ConfigError('suggest requires --run without --suggestion')
            result = suggest_weights(database, args.run, created_at=now())
        else:
            if not args.suggestion or not args.suggestion.strip() or args.run is not None:
                raise ConfigError('show/accept require --suggestion without --run')
            result = (
                show_suggestion(database, args.suggestion)
                if args.action == 'show'
                else accept_suggestion(database, args.suggestion, accepted_at=now())
            )
        print(canonical_json(result))
        return 0
    except (ValueError, ConfigError, RevisionConflict) as error:
        print(f'applicant personalize: {error}', file=sys.stderr)
        return 2
    except (SuggestionNotFound, RankingRunNotFound) as error:
        print(f'applicant personalize: {error}', file=sys.stderr)
        return 4
