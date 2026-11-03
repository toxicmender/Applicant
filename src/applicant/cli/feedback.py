"""Candidate feedback commands and explicit database upgrades."""

import sys
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.feedback import FeedbackDocument, FeedbackInput
from ..errors import ConfigError
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.profiles import RevisionConflict
from ..infra.store.ranking_runs import RankingRunNotFound
from ..infra.store.sqlite import DB_NAME, MIGRATIONS, Store
from ..services.feedback import feedback_report, import_feedback


def nonnegative(value: str) -> int:
    import argparse

    try:
        number = int(value)
        if number >= 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('must be a nonnegative integer')


def add_parser(add):
    command = add('feedback', help='record explicit candidate judgments on completed rankings')
    command.add_argument('action', choices=('record', 'import', 'list'))
    command.add_argument('--database')
    command.add_argument('--run')
    command.add_argument('--job')
    command.add_argument('--event-id')
    command.add_argument('--expected-revision', type=nonnegative)
    command.add_argument('--decision', choices=('shortlist', 'dismiss', 'clear'))
    command.add_argument('--reason', action='append', default=[])
    command.add_argument('--importance', action='append', default=[], metavar='DIMENSION=DIRECTION')
    command.add_argument('--notes', default='')
    command.add_argument('--from-file')
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--history', action='store_true')
    command.add_argument('--feedback-through', type=nonnegative)
    command.add_argument('--json', action='store_true')
    return command


def add_database_parser(add):
    command = add('db', help='explicitly upgrade an existing database')
    command.add_argument('action', choices=('upgrade',))
    command.add_argument('--database', required=True)
    return command


def database_path(args) -> Path:
    if args.settings.store != 'sqlite':
        raise ConfigError('this command requires --store sqlite')
    return (
        Path(args.settings.path(args.database))
        if args.database
        else Path(args.settings.path(args.settings.files.listing)).resolve().parent / DB_NAME
    )


def run_upgrade(args) -> int:
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


def run(args) -> int:
    try:
        database = database_path(args)
        record_options = (
            args.job
            or args.event_id
            or args.expected_revision is not None
            or args.decision
            or args.reason
            or args.importance
            or args.notes
        )
        if args.action == 'list':
            if not args.run or record_options or args.from_file or args.dry_run:
                raise ConfigError('list requires --run and accepts only history/cutoff options')
            result = feedback_report(
                database, args.run, history=args.history, through=args.feedback_through
            )
        else:
            if args.history or args.feedback_through is not None:
                raise ConfigError('history/cutoff require list')
            if args.action == 'import':
                if not args.from_file or record_options or args.run:
                    raise ConfigError('import requires --from-file without record options')
                document = FeedbackDocument.model_validate(
                    read_ranking_document(Path(args.settings.path(args.from_file)))
                )
            else:
                if (
                    args.from_file
                    or not all((args.run, args.job, args.event_id, args.decision))
                    or args.expected_revision is None
                ):
                    raise ConfigError(
                        'record requires --run --job --event-id --decision --expected-revision'
                    )
                importance = {}
                for entry in args.importance:
                    name, direction = entry.split('=', 1)
                    if name in importance:
                        raise ValueError('duplicate importance dimension')
                    importance[name] = direction
                document = FeedbackDocument(
                    events=(
                        FeedbackInput.model_validate(
                            {
                                'event_id': args.event_id,
                                'run_id': args.run,
                                'job_id': args.job,
                                'expected_revision': args.expected_revision,
                                'decision': args.decision,
                                'reasons': args.reason,
                                'importance': importance,
                                'notes': args.notes,
                            }
                        ),
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
