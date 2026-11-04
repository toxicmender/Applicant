"""Strict benchmark imports and offline cohort reports."""

import json
import sys
from pathlib import Path

from ..domain.benchmarks import BenchmarkComparisonDocument, BenchmarkQuery, BenchmarkSnapshot
from ..domain.canonical import canonical_json
from ..errors import ConfigError, StoreError
from ..files import write_document
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.benchmarks import BenchmarkNotFound
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.benchmark_acquisition import discover_benchmarks, fetch_benchmarks
from ..services.benchmarks import compare_benchmarks, import_snapshot, show_snapshots
from ..settings import Settings
from .dispatch import Request
from .feedback import database_path


class ImportSnapshot(Request):
    from_file: str
    dry_run: bool = False

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        snapshot = BenchmarkSnapshot.model_validate(
            read_ranking_document(Path(settings.path(self.from_file)), decimal=True)
        )
        return {
            'kind': 'benchmark_import_result',
            'schema_version': 1,
            'dry_run': self.dry_run,
            'snapshot': import_snapshot(database, snapshot, dry_run=self.dry_run).model_dump(),
        }


def require_database(database: Path) -> None:
    try:
        database.stat()
    except FileNotFoundError:
        raise BenchmarkNotFound(f'database not found: {database}') from None


class ShowSnapshots(Request):
    snapshot: list[str]

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        require_database(database)
        return show_snapshots(database, tuple(self.snapshot))


class CompareSnapshots(Request):
    run: str
    selections: str

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        document = BenchmarkComparisonDocument.model_validate(
            read_ranking_document(Path(settings.path(self.selections)), decimal=True)
        )
        require_database(database)
        return compare_benchmarks(database, self.run, document)


def add_parser(add):
    command = add('benchmark', help='import and compare immutable wage benchmark cohorts offline')
    command.add_argument('action', choices=('import', 'show', 'compare', 'discover', 'fetch'))
    command.add_argument('--provider')
    command.add_argument('--query')
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
        if args.action in ('discover', 'fetch'):
            if (
                args.from_file
                or args.snapshot
                or args.run is not None
                or args.selections
                or args.dry_run
            ):
                raise ConfigError(
                    'acquisition cannot combine with offline input or dry-run options'
                )
            if args.action == 'discover':
                if not args.provider or args.query:
                    raise ConfigError('discover requires --provider without --query')
                report = discover_benchmarks(args.provider)
            else:
                if not args.query or args.provider is not None:
                    raise ConfigError('fetch requires --query; provider is defined by the query')
                query = BenchmarkQuery.model_validate(
                    read_ranking_document(Path(args.settings.path(args.query)), decimal=True)
                )
                report = fetch_benchmarks(database, query)
        elif args.provider is not None or args.query is not None:
            raise ConfigError('--provider/--query require discover/fetch')
        elif args.action == 'import':
            if not args.from_file or args.snapshot or args.run is not None or args.selections:
                raise ConfigError('import requires --from-file without snapshot/run/selections')
            report = ImportSnapshot(from_file=args.from_file, dry_run=args.dry_run).report(
                database, args.settings
            )
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
            request = (
                ShowSnapshots(snapshot=args.snapshot)
                if args.action == 'show'
                else CompareSnapshots(run=args.run, selections=args.selections)
            )
            report = request.report(database, args.settings)
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
