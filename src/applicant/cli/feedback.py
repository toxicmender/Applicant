"""Candidate feedback commands and explicit database upgrades."""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import NonNegativeInt, model_validator

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.feedback import FeedbackDocument, FeedbackInput
from ..errors import ConfigError
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.profiles import RevisionConflict
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.feedback import feedback_report, import_feedback
from ..settings import Settings
from .database import database_path
from .dispatch import AddParser, DatabaseRequest, Identifier, actions, bind


class Record(DatabaseRequest):
    event: FeedbackInput
    dry_run: bool = False

    @model_validator(mode='before')
    @classmethod
    def collect_event(cls, fields: dict[str, Any]) -> dict[str, Any]:
        if 'event' in fields:
            return fields
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


def add_parser(
    add: AddParser, handler: Callable[[Record | ImportFeedback | ListFeedback, Settings], int]
) -> None:
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
    bind(command, Record, handler)
    command = leaf('import', 'import a feedback document')
    command.add_argument('--from-file', required=True)
    command.add_argument('--dry-run', action='store_true')
    bind(command, ImportFeedback, handler)
    command = leaf('list', 'read the judgments for a completed run')
    command.add_argument('--run', required=True)
    command.add_argument('--history', action='store_true')
    command.add_argument('--feedback-through', type=int)
    bind(command, ListFeedback, handler)


def run(args: Record | ImportFeedback | ListFeedback, settings: Settings) -> int:
    try:
        database = database_path(settings, args.database)
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
