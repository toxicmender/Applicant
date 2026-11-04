"""Descriptive evaluation of explicit candidate judgments."""

import json
import sys
import argparse

from pydantic import NonNegativeInt, PositiveInt

from ..domain.canonical import canonical_json
from ..errors import ConfigError, StoreError
from ..files import write_document
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.evaluation import evaluate_run
from ..settings import Settings
from .database import database_path, export_path
from .dispatch import AddParser, DatabaseRequest, Identifier


class Evaluate(DatabaseRequest):
    run: Identifier
    top: PositiveInt = 20
    feedback_through: NonNegativeInt | None = None
    export: str | None = None


def add_parser(add: AddParser) -> argparse.ArgumentParser:
    command = add('evaluate', help='evaluate recorded candidate judgments offline')
    command.add_argument('--run', required=True)
    command.add_argument('--database')
    command.add_argument('--top', type=int, default=20)
    command.add_argument('--feedback-through', type=int)
    command.add_argument('--export')
    command.add_argument('--json', action='store_true')
    return command


def run(args: Evaluate, settings: Settings) -> int:
    try:
        database = database_path(settings, args.database)
        target = export_path(settings, database, args.export)
        report = evaluate_run(database, args.run, top=args.top, through=args.feedback_through)
        if target is not None:
            try:
                write_document(
                    str(target), json.loads(canonical_json(report)), trailing_newline=True
                )
            except OSError as error:
                raise StoreError(f'could not export evaluation: {error}') from error
        print(canonical_json(report))
        return 0
    except (ValueError, ConfigError) as error:
        print(f'applicant evaluate: {error}', file=sys.stderr)
        return 2
    except RankingRunNotFound as error:
        print(f'applicant evaluate: {error}', file=sys.stderr)
        return 4
