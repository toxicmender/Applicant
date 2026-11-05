"""Create a ranking run from validated inputs and display its completed results."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import PositiveInt

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.ranking_inputs import ProvenanceDocument
from ..errors import ConfigError
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.ranking_runs import RankingRunNotFound
from ..services.evaluation import compare_history
from ..services.ranking_cli import create_ranking_run
from ..services.ranking_read import RankingJobNotFound, list_runs, read_run
from ..settings import Settings
from .common import positive_int
from .database import database_path
from .dispatch import AddParser, DatabaseRequest, Identifier, actions, bind
from .render import shown


class Display(DatabaseRequest):
    top: PositiveInt = 20
    all: bool = False

    @property
    def limit(self) -> int | None:
        return None if self.all else self.top


class Create(Display):
    provenance: str
    profile: Identifier = 'default'
    revision: PositiveInt | None = None
    input: str | None = None
    job_versions: str | None = None
    scoring_config: str | None = None
    tiers: str | None = None
    offline: bool = False


class ListRuns(DatabaseRequest):
    profile: Identifier | None = None


class Show(Display):
    run_id: Identifier


class Explain(DatabaseRequest):
    run_id: Identifier
    job_id: Identifier


class Compare(DatabaseRequest):
    base: Identifier
    candidate: Identifier


class Replay(DatabaseRequest):
    run_id: Identifier
    provenance: str


def add_parser(
    add: AddParser,
    handler: Callable[[Create | ListRuns | Show | Explain | Compare | Replay, Settings], int],
) -> None:
    action = actions(add('rank', help='create, inspect and verify ranking runs'))

    def leaf(name: str, help: str) -> argparse.ArgumentParser:
        command = action(name, help=help)
        command.add_argument('--database')
        command.add_argument('--json', action='store_true')
        return command

    def limits(command: argparse.ArgumentParser) -> None:
        group = command.add_mutually_exclusive_group()
        group.add_argument(
            '--top', type=positive_int, default=20, help='results per eligibility group'
        )
        group.add_argument('--all', action='store_true', help='show every result')

    command = leaf('create', 'create an immutable ranking run')
    command.add_argument('--provenance', required=True)
    command.add_argument('--profile', default='default')
    command.add_argument('--revision', type=positive_int)
    inputs = command.add_mutually_exclusive_group()
    inputs.add_argument('--input', help='listing JSON; defaults to the configured listing')
    inputs.add_argument('--job-versions', help='frozen job-version bundle')
    command.add_argument('--scoring-config')
    command.add_argument('--tiers')
    command.add_argument('--offline', action='store_true')
    limits(command)
    bind(command, Create, handler)
    command = leaf('list', 'list completed ranking runs offline')
    command.add_argument('--profile')
    bind(command, ListRuns, handler)
    command = leaf('show', 'display a completed ranking run')
    command.add_argument('run_id', metavar='RUN_ID')
    limits(command)
    bind(command, Show, handler)
    command = leaf('explain', 'explain one job in a completed run')
    command.add_argument('run_id', metavar='RUN_ID')
    command.add_argument('job_id', metavar='JOB_ID')
    bind(command, Explain, handler)
    command = leaf('compare', 'compare two historical runs')
    command.add_argument('base', metavar='BASE_RUN')
    command.add_argument('candidate', metavar='CANDIDATE_RUN')
    bind(command, Compare, handler)
    command = leaf('replay', 'verify a completed run offline against its provenance')
    command.add_argument('run_id', metavar='RUN_ID')
    command.add_argument('--provenance', required=True)
    bind(command, Replay, handler)


def _render(report: dict[str, Any], database: Path) -> None:
    print(f'run {report["run"]["ranking_run_id"]}; database: {shown(database.resolve())}')
    if 'legacy' in report:
        legacy = report['legacy']
        print(legacy['notice'])
        print(f'{legacy["displayed"]} displayed / {legacy["total"]} total')
        for item in legacy['results']:
            print(
                f'  {item["job_version_id"]} {shown(item["job"].get("title"))}: {shown(canonical_json(item["ranking"]))}'
            )
        return
    print(
        'Fit is preference alignment; confidence is evidence support, not a hiring probability. Coverage is the known share of applicable weight.'
    )
    for state, group in report['groups'].items():
        print(f'{state}: {group["displayed"]} displayed / {group["total"]} total')
        for result in group['results']:
            fit = (
                'unscored'
                if result['overall_score'] is None
                else format(result['overall_score'], '.2f')
            )
            print(
                f'  {result["job_id"]} {shown(result["job"].get("title"))}: fit={fit}, confidence={result["confidence"]}, coverage={result["coverage"]}'
            )
            for criterion in result['decision']['criteria']:
                if criterion['status'] in ('fail', 'unknown', 'not_applicable'):
                    print(
                        '    {}: {}'.format(
                            shown(criterion['reason_code']), shown(criterion['explanation'])[:180]
                        )
                    )
    for diagnostic in report['run']['resolution_diagnostics']:
        print(f'FX: {shown(diagnostic)}')


def _display(report: dict[str, Any], database: Path) -> None:
    if report['kind'] == 'ranking_run_list':
        if not report['runs']:
            print('no completed ranking runs')
        for item in report['runs']:
            print(
                shown(
                    f'{item["ranking_run_id"]}: {item["profile_id"]}/{item["profile_revision"]} at {item["calculated_at"]} ({item["job_count"]} jobs)'
                )
            )
    elif report['kind'] in ('ranking_explanation', 'ranking_comparison'):
        for line in json.dumps(
            json.loads(canonical_json(report)), ensure_ascii=False, indent=2
        ).splitlines():
            print(shown(line))
    elif report['kind'] == 'ranking_replay_report':
        verification = report['verification']
        print(
            f'replay {shown(verification["ranking_run_id"])}: {"matches" if verification["matches"] else "mismatch"}'
        )
        for key in verification['mismatched_job_version_ids']:
            print(f'  mismatched job version: {shown(key)}')
    else:
        _render(report, database)


def run(args: Create | ListRuns | Show | Explain | Compare | Replay, settings: Settings) -> int:
    try:
        database = database_path(settings, args.database)
        if isinstance(args, Compare):
            report = compare_history(database, args.base, args.candidate)
        elif isinstance(args, ListRuns):
            report = list_runs(database, profile_id=args.profile.strip() if args.profile else None)
        elif isinstance(args, Show):
            report = read_run(database, args.run_id.strip(), top=args.limit)
        elif isinstance(args, Explain):
            report = read_run(database, args.run_id.strip(), job_id=args.job_id)
        elif isinstance(args, Replay):
            try:
                provenance = ProvenanceDocument.model_validate(
                    read_ranking_document(Path(settings.path(args.provenance)))
                )
            except ValueError as error:
                raise ConfigError(f'invalid provenance: {error}') from error
            report = read_run(database, args.run_id.strip(), provenance=provenance)
        else:

            def path(value: str | None) -> Path | None:
                return None if value is None else Path(settings.path(value))

            created = create_ranking_run(
                database,
                listing=Path(
                    settings.path(settings.files.listing if args.input is None else args.input)
                ),
                provenance_path=Path(settings.path(args.provenance)),
                versions_path=path(args.job_versions),
                config_path=path(args.scoring_config),
                tiers_path=path(args.tiers),
                profile_id=args.profile.strip(),
                revision=args.revision,
                offline=args.offline,
                clock=now,
            )
            report = read_run(database, created.ranking_run_id, top=args.limit)
        if args.json_output:
            print(canonical_json(report))
        else:
            _display(report, database)
        return (
            1
            if report['kind'] == 'ranking_replay_report' and not report['verification']['matches']
            else 0
        )
    except (ValueError, ConfigError) as error:
        print(f'applicant rank: {error}', file=sys.stderr)
        return 2
    except (RankingRunNotFound, RankingJobNotFound) as error:
        print(f'applicant rank: {error}', file=sys.stderr)
        return 4
