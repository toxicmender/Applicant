"""Create a ranking run from validated inputs and display its completed results."""

import sys
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.ranking_report import ranking_report
from ..domain.ranking_v2 import ScoringConfigurationV2
from ..errors import ConfigError
from ..infra.store.sqlite import DB_NAME, Store
from ..services.ranking_cli import create_ranking_run
from ..services.ranking_history import load_history
from .common import positive_int
from .render import shown


def add_parser(add):
    command = add('rank', help='create an explainable ranking run from captured jobs')
    command.add_argument(
        '--provenance',
        required=True,
        metavar='PATH',
        help='retained artifact build identity and lockfile digest JSON',
    )
    command.add_argument(
        '--database', metavar='PATH', help='database; default beside the configured listing'
    )
    command.add_argument(
        '--profile', default='default', help='candidate profile ID (default: default)'
    )
    command.add_argument(
        '--revision', type=positive_int, help='exact profile revision (default: current)'
    )
    inputs = command.add_mutually_exclusive_group()
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


def run(args) -> int:
    try:
        if args.settings.store != 'sqlite':
            raise ConfigError('rank requires --store sqlite for immutable ranking history')
        if not args.profile.strip():
            raise ConfigError('--profile must not be blank')
        settings = args.settings
        configured = Path(settings.path(settings.files.listing))
        database = (
            Path(settings.path(args.database))
            if args.database is not None
            else configured.resolve().parent / DB_NAME
        )

        def path(value):
            return None if value is None else Path(settings.path(value))

        run = create_ranking_run(
            database,
            listing=configured if args.input is None else Path(settings.path(args.input)),
            provenance_path=Path(settings.path(args.provenance)),
            versions_path=path(args.job_versions),
            config_path=path(args.scoring_config),
            tiers_path=path(args.tiers),
            profile_id=args.profile.strip(),
            revision=args.revision,
            offline=args.offline,
            clock=now,
        )
        with Store.open_readonly(database) as reader, reader.read_snapshot():
            history = load_history(reader, run.ranking_run_id)
        report = ranking_report(
            run,
            history.jobs,
            config=ScoringConfigurationV2.model_validate(history.config.model_dump()),
            top=None if args.all else (args.top or 20),
        )
        if args.json:
            print(canonical_json(report))
        else:
            _render(report, database)
        return 0
    except ConfigError as error:
        print(f'applicant rank: {error}', file=sys.stderr)
        return 2
