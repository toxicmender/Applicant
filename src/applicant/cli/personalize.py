"""Explicit generation, inspection and acceptance of weight suggestions."""

import argparse
import sys

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..errors import ConfigError
from ..infra.store.personalization import SuggestionNotFound
from ..infra.store.profiles import RevisionConflict
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.personalization import accept_suggestion, show_suggestion, suggest_weights
from ..settings import Settings
from .database import database_path
from .dispatch import AddParser, DatabaseRequest, Identifier, actions, bind


class Suggest(DatabaseRequest):
    run: Identifier


class Show(DatabaseRequest):
    suggestion: Identifier


class Accept(DatabaseRequest):
    suggestion: Identifier


def add_parser(add: AddParser) -> None:
    action = actions(add('personalize', help='preview and explicitly accept weight suggestions'))

    def leaf(name: str, help: str) -> argparse.ArgumentParser:
        command = action(name, help=help)
        command.add_argument('--database')
        command.add_argument('--json', action='store_true')
        return command

    command = leaf('suggest', 'generate a weight suggestion from a ranked run')
    command.add_argument('--run', required=True)
    bind(command, Suggest, run)
    for name, model in (('show', Show), ('accept', Accept)):
        command = leaf(
            name,
            'read a saved suggestion'
            if name == 'show'
            else 'accept a suggestion as a new profile revision',
        )
        command.add_argument('--suggestion', required=True)
        bind(command, model, run)


def run(args: Suggest | Show | Accept, settings: Settings) -> int:
    try:
        database = database_path(settings, args.database)
        if isinstance(args, Suggest):
            result = suggest_weights(database, args.run, created_at=now())
        elif isinstance(args, Show):
            result = show_suggestion(database, args.suggestion)
        else:
            result = accept_suggestion(database, args.suggestion, accepted_at=now())
        print(canonical_json(result))
        return 0
    except (ValueError, ConfigError, RevisionConflict) as error:
        print(f'applicant personalize: {error}', file=sys.stderr)
        return 2
    except (SuggestionNotFound, RankingRunNotFound) as error:
        print(f'applicant personalize: {error}', file=sys.stderr)
        return 4
