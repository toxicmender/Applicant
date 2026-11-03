"""Create a ranking run from validated inputs and display its completed results."""

import json
import sys
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.ranking_inputs import ProvenanceDocument
from ..errors import ConfigError
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.ranking_runs import RankingRunNotFound
from ..infra.store.sqlite import DB_NAME
from ..services.evaluation import compare_history
from ..services.ranking_cli import create_ranking_run
from ..services.ranking_read import RankingJobNotFound, list_runs, read_run
from .common import positive_int
from .render import shown


def add_parser(add):
    command = add('rank', help='create an explainable ranking run from captured jobs')
    command.add_argument(
        '--provenance',
        metavar='PATH',
        help='retained artifact build identity and lockfile digest JSON',
    )
    command.add_argument(
        '--database', metavar='PATH', help='database; default beside the configured listing'
    )
    command.add_argument(
        '--profile', help='candidate profile ID (creation default: default; list default: all)'
    )
    command.add_argument(
        '--revision', type=positive_int, help='exact profile revision (default: current)'
    )
    inputs = command.add_mutually_exclusive_group()
    modes = command.add_mutually_exclusive_group()
    modes.add_argument('--run', metavar='RUN_ID', help='display an exact completed run offline')
    modes.add_argument(
        '--compare',
        nargs=2,
        metavar=('BASE_RUN', 'CANDIDATE_RUN'),
        help='compare exact historical runs offline',
    )
    modes.add_argument(
        '--replay', metavar='RUN_ID', help='verify original inputs offline using --provenance'
    )
    modes.add_argument(
        '--list-runs', action='store_true', help='list completed history without creating a run'
    )
    command.add_argument(
        '--explain',
        metavar='JOB_ID',
        help='explain a member of --run using its full ranking job ID',
    )
    inputs.add_argument(
        '--input', metavar='PATH', help='ordinary listing JSON (default: configured listing)'
    )
    inputs.add_argument(
        '--job-versions', metavar='PATH', help='ordered frozen job-version bundle JSON'
    )
    command.add_argument(
        '--scoring-config', metavar='PATH', help='full scoring configuration v2 JSON'
    )
    command.add_argument('--tiers', metavar='PATH', help='exact job-ID to salary-tier JSON map')
    command.add_argument(
        '--offline',
        action='store_true',
        help='resolve FX from eligible captured cache or identity only',
    )
    limits = command.add_mutually_exclusive_group()
    limits.add_argument(
        '--top',
        type=positive_int,
        help='display up to N results per eligibility group (default: 20)',
    )
    limits.add_argument('--all', action='store_true', help='display all results')
    command.add_argument(
        '--json',
        action='store_true',
        help='canonical JSON report with explicit eligibility and nullable scores',
    )
    return command


def _render(report: dict, database: Path) -> None:
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


def _validate(args) -> None:
    if args.compare is not None:
        if any(
            (
                args.provenance,
                args.profile,
                args.revision,
                args.explain,
                args.input,
                args.job_versions,
                args.scoring_config,
                args.tiers,
                args.offline,
                args.top is not None,
                args.all,
            )
        ) or not all(v.strip() for v in args.compare):
            raise ConfigError('--compare accepts only database and JSON options')
        return
    for name in ('profile', 'run', 'replay', 'explain'):
        value = getattr(args, name)
        if value is not None and not value.strip():
            raise ConfigError(f'--{name} must not be blank')
    creation = any(
        (
            args.input is not None,
            args.job_versions is not None,
            args.scoring_config is not None,
            args.tiers is not None,
            args.revision is not None,
            args.offline,
        )
    )
    if args.explain is not None and (args.run is None or args.top is not None or args.all):
        raise ConfigError('--explain requires --run and cannot combine with --top/--all')
    if args.run is not None and (
        creation or args.provenance is not None or args.profile is not None
    ):
        raise ConfigError('--run cannot combine with creation options or --profile')
    if args.replay is not None or args.list_runs:
        if creation or args.top is not None or args.all or args.explain is not None:
            raise ConfigError(
                'replay/list modes cannot combine with creation or display-limit options'
            )
        if args.replay is not None and args.profile is not None:
            raise ConfigError('--replay cannot combine with --profile')
        if args.list_runs and args.provenance is not None:
            raise ConfigError('--list-runs cannot combine with --provenance')
    if args.run is None and not args.list_runs and args.provenance is None:
        raise ConfigError('creation and replay require --provenance PATH')


def _display(report: dict, database: Path) -> None:
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


def run(args) -> int:
    try:
        if args.settings.store != 'sqlite':
            raise ConfigError('rank requires --store sqlite for immutable ranking history')
        _validate(args)
        settings = args.settings
        configured = Path(settings.path(settings.files.listing))
        database = (
            Path(settings.path(args.database))
            if args.database is not None
            else configured.resolve().parent / DB_NAME
        )

        def path(value):
            return None if value is None else Path(settings.path(value))

        if args.compare is not None:
            report = compare_history(database, *args.compare)
        elif args.list_runs:
            report = list_runs(
                database, profile_id=None if args.profile is None else args.profile.strip()
            )
        elif args.run is not None or args.replay is not None:
            provenance = None
            if args.replay is not None:
                try:
                    provenance = ProvenanceDocument.model_validate(
                        read_ranking_document(Path(settings.path(args.provenance)))
                    )
                except ValueError as error:
                    raise ConfigError(f'invalid provenance: {error}') from error
            requested = args.run or args.replay
            assert requested is not None
            report = read_run(
                database,
                requested.strip(),
                top=None if args.all else (args.top or 20),
                job_id=args.explain,
                provenance=provenance,
            )
        else:
            created = create_ranking_run(
                database,
                listing=configured if args.input is None else Path(settings.path(args.input)),
                provenance_path=Path(settings.path(args.provenance)),
                versions_path=path(args.job_versions),
                config_path=path(args.scoring_config),
                tiers_path=path(args.tiers),
                profile_id=(args.profile or 'default').strip(),
                revision=args.revision,
                offline=args.offline,
                clock=now,
            )
            report = read_run(
                database, created.ranking_run_id, top=None if args.all else (args.top or 20)
            )
        if args.json:
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
