"""Strict benchmark imports and offline cohort reports."""

import argparse
import json
import sys
from pathlib import Path

from typing import Literal, get_args

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
from .database import database_path, export_path
from .dispatch import AddParser, DatabaseRequest, Identifier, actions, bind


class BenchmarkRequest(DatabaseRequest):
    export: str | None = None


class ImportSnapshot(BenchmarkRequest):
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


class ShowSnapshots(BenchmarkRequest):
    snapshot: list[Identifier]

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        require_database(database)
        return show_snapshots(database, tuple(self.snapshot))


class CompareSnapshots(BenchmarkRequest):
    run: Identifier
    selections: str

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        document = BenchmarkComparisonDocument.model_validate(
            read_ranking_document(Path(settings.path(self.selections)), decimal=True)
        )
        require_database(database)
        return compare_benchmarks(database, self.run, document)


Provider = Literal['bls', 'eurostat', 'mospi']


class Discover(BenchmarkRequest):
    provider: Provider

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        return discover_benchmarks(self.provider)


class Fetch(BenchmarkRequest):
    query: str

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        query = BenchmarkQuery.model_validate(
            read_ranking_document(Path(settings.path(self.query)), decimal=True)
        )
        return fetch_benchmarks(database, query)


Benchmark = ImportSnapshot | ShowSnapshots | CompareSnapshots | Discover | Fetch


def add_parser(add: AddParser) -> None:
    group = add('benchmark', help='import and compare wage benchmark cohorts')
    action = actions(group)

    def leaf(name: str, help: str) -> argparse.ArgumentParser:
        command = action(name, help=help)
        command.add_argument('--database')
        command.add_argument('--export')
        command.add_argument('--json', action='store_true')
        return command

    command = leaf('import', 'import an immutable benchmark snapshot')
    command.add_argument('--from-file', required=True)
    command.add_argument('--dry-run', action='store_true')
    bind(command, ImportSnapshot, run)
    command = leaf('show', 'display stored benchmark snapshots')
    command.add_argument('--snapshot', action='append', required=True)
    bind(command, ShowSnapshots, run)
    command = leaf('compare', 'compare a ranking run against selected benchmarks')
    command.add_argument('--run', required=True)
    command.add_argument('--selections', required=True)
    bind(command, CompareSnapshots, run)
    command = leaf('discover', 'discover available provider datasets')
    command.add_argument('--provider', required=True, choices=get_args(Provider))
    bind(command, Discover, run)
    command = leaf('fetch', 'fetch the observations specified by a query document')
    command.add_argument('--query', required=True)
    bind(command, Fetch, run)


def run(args: Benchmark, settings: Settings) -> int:
    try:
        database = database_path(settings, args.database)
        target = export_path(settings, database, args.export)
        report = args.report(database, settings)
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
