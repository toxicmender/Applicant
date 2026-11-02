"""Noninteractive candidate survey commands."""

import sys
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..errors import ConfigError
from ..infra.preference_documents import export_preferences, read_preferences
from ..infra.store.profiles import ProfileNotFound, RevisionConflict
from ..infra.store.sqlite import DB_NAME
from ..services.survey import editable_profile, import_preferences, load_profile
from .common import positive_int


def add_parser(add):
    command = add('survey', help='validate, import, show or export candidate preferences')
    modes = command.add_mutually_exclusive_group()
    modes.add_argument('--show', action='store_true', help='show an archived profile')
    modes.add_argument('--export', metavar='PATH', help='export editable preferences')
    modes.add_argument(
        '--from-file', metavar='PATH', help='save editable preferences as a new revision'
    )
    modes.add_argument('--validate', metavar='PATH', help='validate without opening a database')
    command.add_argument(
        '--dry-run',
        action='store_true',
        help='check --from-file against the current revision without saving',
    )
    command.add_argument(
        '--profile', metavar='ID', help='profile to show/export (default: default)'
    )
    command.add_argument('--revision', type=positive_int, help='exact revision to show/export')
    command.add_argument('--json', action='store_true', help='canonical archived JSON with --show')
    command.add_argument(
        '--database', metavar='PATH', help='database path; default beside the configured listing'
    )
    return command


def run(args) -> int:
    try:
        if args.settings.store != 'sqlite':
            raise ConfigError('survey requires --store sqlite for versioned profiles')
        if args.dry_run and not args.from_file:
            raise ConfigError('--dry-run requires --from-file')
        if args.revision is not None and not (args.show or args.export):
            raise ConfigError('--revision requires --show or --export')
        if args.json and not args.show:
            raise ConfigError('--json requires --show')
        if args.profile is not None and (args.from_file or args.validate):
            raise ConfigError('--profile cannot override an imported document identity')
        if args.profile is not None and not args.profile.strip():
            raise ConfigError('--profile must not be blank')
        settings = args.settings
        database = (
            Path(settings.path(args.database))
            if args.database is not None
            else Path(settings.path(settings.files.listing)).resolve().parent / DB_NAME
        )
        if args.validate:
            document = read_preferences(settings.path(args.validate))
            print(
                f'valid preferences: {document.profile_id}, expected revision {document.expected_revision}'
            )
        elif args.from_file:
            result = import_preferences(
                database,
                read_preferences(settings.path(args.from_file)),
                updated_at=now(),
                dry_run=args.dry_run,
            )
            print(
                f'{"proposed" if args.dry_run else "saved"} profile {result.profile_id} revision {result.profile_revision}; database: {database.resolve()}'
            )
            if args.dry_run:
                print(canonical_json(result))
        elif args.show or args.export:
            result = load_profile(database, (args.profile or 'default').strip(), args.revision)
            if args.export:
                target = settings.path(args.export)
                export_preferences(target, editable_profile(result), database=database)
                print(
                    f'exported profile {result.profile_id} revision {result.profile_revision} to {target}; database: {database.resolve()}'
                )
            elif args.json:
                print(canonical_json(result))
            else:
                print(
                    f'profile {result.profile_id} revision {result.profile_revision}; database: {database.resolve()}'
                )
                print(canonical_json(editable_profile(result).expanded()))
                print(
                    'Salary is annual base pay. Required modes constrain eligibility; preferred modes affect fit.'
                )
                print(
                    'Experience, discipline, seniority, regions and timezone are retained but currently do not add ranking signals.'
                )
        else:
            raise ConfigError(
                'use --from-file PATH to save preferences; interactive survey is not implemented yet'
            )
        return 0
    except (ConfigError, RevisionConflict) as error:
        print(f'applicant survey: {error}', file=sys.stderr)
        return 2
    except ProfileNotFound as error:
        print(f'applicant survey: {error}', file=sys.stderr)
        return 4
