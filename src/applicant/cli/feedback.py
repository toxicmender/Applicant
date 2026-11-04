"""Candidate feedback commands and explicit database upgrades."""

import argparse
import sys
from typing import Any

from pydantic import NonNegativeInt, model_validator
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.feedback import FeedbackDocument, FeedbackInput
from ..errors import ConfigError
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.profiles import RevisionConflict
from ..infra.store.ranking_runs import RankingRunNotFound
from ..infra.store.sqlite import MIGRATIONS, Store
from ..services.feedback import feedback_report, import_feedback
from ..settings import Settings
from .database import database_path as resolve_database
from .dispatch import AddParser, DatabaseRequest, Identifier, actions, bind


def nonnegative(value: str) -> int:
    try:
        number = int(value)
        if number >= 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('must be a nonnegative integer')


class Record(DatabaseRequest):
    event: FeedbackInput
    dry_run: bool = False

    @model_validator(mode='before')
    @classmethod
    def collect_event(cls, fields: dict[str, Any]) -> dict[str, Any]:
        values = fields.copy()
        event = {
            key: values.pop(key)
            for key in (
                'run_id',
                'job_id',
                'event_id',
                'expected_revision',
                'decision',
                'reasons',
                'notes',
            )
        }
        importance: dict[str, str] = {}
        for entry in values.pop('importance'):
            name, separator, direction = entry.partition('=')
            if not separator or name in importance:
                raise ValueError('use --importance DIMENSION=DIRECTION once per dimension')
            importance[name] = direction
        return {**values, 'event': {**event, 'importance': importance}}


class ImportFeedback(DatabaseRequest):
    from_file: str
    dry_run: bool = False


class ListFeedback(DatabaseRequest):
    run: Identifier
    history: bool = False
    feedback_through: NonNegativeInt | None = None


def add_parser(add: AddParser) -> None:
    action = actions(add('feedback', help='record candidate judgments on completed rankings'))

    def leaf(name: str, help: str) -> argparse.ArgumentParser:
        command = action(name, help=help)
        command.add_argument('--database')
        command.add_argument('--json', action='store_true')
        return command

    command = leaf('record', 'record one judgment at an expected revision')
    command.add_argument('--run', dest='run_id', required=True)
    command.add_argument('--job', dest='job_id', required=True)
    command.add_argument('--event-id', required=True)
    command.add_argument('--expected-revision', type=int, required=True)
    command.add_argument('--decision', choices=('shortlist', 'dismiss', 'clear'), required=True)
    command.add_argument('--reason', dest='reasons', action='append', default=[])
    command.add_argument('--importance', action='append', default=[], metavar='DIMENSION=DIRECTION')
    command.add_argument('--notes', default='')
    command.add_argument('--dry-run', action='store_true')
    bind(command, Record, run)
    command = leaf('import', 'import a feedback document')
    command.add_argument('--from-file', required=True)
    command.add_argument('--dry-run', action='store_true')
    bind(command, ImportFeedback, run)
    command = leaf('list', 'read the judgments for a completed run')
    command.add_argument('--run', required=True)
    command.add_argument('--history', action='store_true')
    command.add_argument('--feedback-through', type=int)
    bind(command, ListFeedback, run)


def add_database_parser(add: AddParser) -> argparse.ArgumentParser:
    command = add('db', help='explicitly upgrade an existing database')
    command.add_argument('action', choices=('upgrade',))
    command.add_argument('--database', required=True)
    return command


def database_path(args: argparse.Namespace) -> Path:
    return resolve_database(args.settings, args.database)


def run_upgrade(args: argparse.Namespace) -> int:
    try:
        database = database_path(args)
        if not database.is_file():
            print(f'database not found: {database}', file=sys.stderr)
            return 4
        with Store(database):
            pass
        print(f'database upgraded to schema {len(MIGRATIONS)}: {database.resolve()}')
        return 0
    except ConfigError as error:
        print(f'applicant db: {error}', file=sys.stderr)
        return 2


def run(args: Record | ImportFeedback | ListFeedback, settings: Settings) -> int:
    try:
        database = resolve_database(settings, args.database)
        if isinstance(args, ListFeedback):
            result = feedback_report(
                database, args.run, history=args.history, through=args.feedback_through
            )
        else:
            document = (
                FeedbackDocument(events=(args.event,))
                if isinstance(args, Record)
                else FeedbackDocument.model_validate(
                    read_ranking_document(Path(settings.path(args.from_file)))
                )
            )
            events = import_feedback(database, document, recorded_at=now(), dry_run=args.dry_run)
            result = {
                'kind': 'candidate_feedback_import_result',
                'schema_version': 1,
                'dry_run': args.dry_run,
                'events': [e.model_dump() for e in events],
            }
        print(canonical_json(result))
        return 0
    except (ValueError, ConfigError, RevisionConflict) as error:
        print(f'applicant feedback: {error}', file=sys.stderr)
        return 2
    except RankingRunNotFound as error:
        print(f'applicant feedback: {error}', file=sys.stderr)
        return 4
