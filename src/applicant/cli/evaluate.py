"""Descriptive evaluation of explicit candidate judgments."""

import json
import sys
from pathlib import Path

from ..domain.canonical import canonical_json
from ..errors import ConfigError, StoreError
from ..files import write_document
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.evaluation import evaluate_run
from .common import positive_int
from .feedback import database_path, nonnegative


def add_parser(add):
    command = add('evaluate', help='evaluate recorded candidate judgments offline')
    command.add_argument('--run', required=True)
    command.add_argument('--database')
    command.add_argument('--top', type=positive_int, default=20)
    command.add_argument('--feedback-through', type=nonnegative)
    command.add_argument('--export')
    command.add_argument('--json', action='store_true')
    return command


def run(args) -> int:
    try:
        database = database_path(args)
        if not args.run.strip():
            raise ConfigError('--run must not be blank')
        report = evaluate_run(database, args.run, top=args.top, through=args.feedback_through)
        if args.export:
            target = Path(args.settings.path(args.export)).resolve()
            if target in {
                database.resolve(),
                *(Path(str(database) + s).resolve() for s in ('-wal', '-shm')),
            }:
                raise ConfigError('export must not replace database or sidecars')
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
