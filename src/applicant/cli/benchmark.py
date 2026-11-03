"""Strict benchmark imports and offline cohort reports."""

import json
import sys
from pathlib import Path

from ..domain.benchmarks import BenchmarkComparisonDocument, BenchmarkSnapshot
from ..domain.canonical import canonical_json
from ..errors import ConfigError, StoreError
from ..files import write_document
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.benchmarks import BenchmarkNotFound
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.benchmarks import compare_benchmarks, import_snapshot, show_snapshots
from .feedback import database_path


def add_parser(add):
    command = add('benchmark', help='import and compare immutable wage benchmark cohorts offline')
    command.add_argument('action', choices=('import', 'show', 'compare'))
    command.add_argument('--database')
    command.add_argument('--from-file')
    command.add_argument('--snapshot', action='append', default=[])
    command.add_argument('--run')
    command.add_argument('--selections')
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--export')
    command.add_argument('--json', action='store_true')
    return command


def run(args) -> int:
    try:
        database = database_path(args)
        target = Path(args.settings.path(args.export)).resolve() if args.export else None
        if target in {
            database.resolve(),
            *(Path(str(database) + s).resolve() for s in ('-wal', '-shm')),
        }:
            raise ConfigError('export must not replace database or sidecars')
        if args.action == 'import':
            if not args.from_file or args.snapshot or args.run is not None or args.selections:
                raise ConfigError('import requires --from-file without snapshot/run/selections')
            snapshot = BenchmarkSnapshot.model_validate(
                read_ranking_document(Path(args.settings.path(args.from_file)), decimal=True)
            )
            report = {
                'kind': 'benchmark_import_result',
                'schema_version': 1,
                'dry_run': args.dry_run,
                'snapshot': import_snapshot(database, snapshot, dry_run=args.dry_run).model_dump(),
            }
        else:
            if args.from_file or args.dry_run:
                raise ConfigError('--from-file and --dry-run require import')
            if args.action == 'show':
                if (
                    not args.snapshot
                    or any(not v.strip() for v in args.snapshot)
                    or args.run is not None
                    or args.selections
                ):
                    raise ConfigError('show requires --snapshot without run/selections')
            elif not args.run or not args.run.strip() or not args.selections or args.snapshot:
                raise ConfigError('compare requires --run --selections without --snapshot')
            document = (
                BenchmarkComparisonDocument.model_validate(
                    read_ranking_document(Path(args.settings.path(args.selections)), decimal=True)
                )
                if args.action == 'compare'
                else None
            )
            try:
                database.stat()
            except FileNotFoundError:
                raise BenchmarkNotFound(f'database not found: {database}') from None
            if document is None:
                report = show_snapshots(database, tuple(args.snapshot))
            else:
                assert args.run is not None
                report = compare_benchmarks(database, args.run, document)
        if target is not None:
            try:
                write_document(
                    str(target), json.loads(canonical_json(report)), trailing_newline=True
                )
            except OSError as error:
                raise StoreError(f'could not export benchmarks: {error}') from error
        print(canonical_json(report))
        return 0
    except (ValueError, ConfigError) as error:
        print(f'applicant benchmark: {error}', file=sys.stderr)
        return 2
    except (BenchmarkNotFound, RankingRunNotFound) as error:
        print(f'applicant benchmark: {error}', file=sys.stderr)
        return 4
