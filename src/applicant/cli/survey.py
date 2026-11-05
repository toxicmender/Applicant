"""Interactive and document-based candidate survey commands."""

import sys
from collections.abc import Callable

from pydantic import Field, PositiveInt

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..errors import ConfigError
from ..infra.preference_documents import export_preferences, read_preferences
from ..infra.store.profiles import ProfileNotFound, RevisionConflict
from ..interaction import Terminal
from ..services.survey import editable_profile, import_preferences, load_profile
from ..services.survey_questionnaire import questionnaire
from ..settings import Settings
from .database import database_path
from .dispatch import AddParser, Identifier, Request, actions, bind


class Edit(Request):
    database: str | None = None
    profile: Identifier = 'default'


class Show(Edit):
    revision: PositiveInt | None = None
    json_output: bool = Field(default=False, alias='json')


class Export(Edit):
    revision: PositiveInt | None = None
    path: str


class Validate(Request):
    path: str


class Import(Validate):
    database: str | None = None
    dry_run: bool = False


def add_parser(
    add: AddParser, handler: Callable[[Edit | Show | Export | Validate | Import, Settings], int]
) -> None:
    action = actions(add('survey', help='manage versioned candidate preferences'))
    command = action('edit', help='create or edit a profile interactively')
    command.add_argument('--profile', default='default')
    command.add_argument('--database')
    bind(command, Edit, handler)
    command = action('validate', help='validate preferences without opening a database')
    command.add_argument('path', metavar='PATH')
    bind(command, Validate, handler)
    command = action('import', help='save preferences as a new revision')
    command.add_argument('path', metavar='PATH')
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--database')
    bind(command, Import, handler)
    for name, model in (('show', Show), ('export', Export)):
        command = action(
            name,
            help='read an archived profile' if name == 'show' else 'export editable preferences',
        )
        command.add_argument('--profile', default='default')
        command.add_argument('--revision', type=int)
        command.add_argument('--database')
        if name == 'show':
            command.add_argument('--json', action='store_true')
        else:
            command.add_argument('path', metavar='PATH')
        bind(command, model, handler)


def run(args: Edit | Show | Export | Validate | Import, settings: Settings) -> int:
    try:
        if settings.store != 'sqlite':
            raise ConfigError('survey requires --store sqlite for versioned profiles')
        if isinstance(args, Validate) and not isinstance(args, Import):
            document = read_preferences(settings.path(args.path))
            print(
                f'valid preferences: {document.profile_id}, expected revision {document.expected_revision}'
            )
            return 0
        database = database_path(settings, args.database)
        if isinstance(args, Import):
            result = import_preferences(
                database,
                read_preferences(settings.path(args.path)),
                updated_at=now(),
                dry_run=args.dry_run,
            )
            print(
                f'{"proposed" if args.dry_run else "saved"} profile {result.profile_id} revision {result.profile_revision}; database: {database.resolve()}'
            )
            if args.dry_run:
                print(canonical_json(result))
        elif isinstance(args, (Show, Export)):
            result = load_profile(database, args.profile.strip(), args.revision)
            if isinstance(args, Export):
                target = settings.path(args.path)
                export_preferences(target, editable_profile(result), database=database)
                print(
                    f'exported profile {result.profile_id} revision {result.profile_revision} to {target}; database: {database.resolve()}'
                )
            elif args.json_output:
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
            if not sys.stdin.isatty():
                raise ConfigError(
                    'interactive survey requires a terminal; use applicant survey import PATH'
                )
            profile_id = args.profile.strip()
            try:
                initial = editable_profile(load_profile(database, profile_id))
            except ProfileNotFound:
                initial = None
            document = questionnaire(Terminal(), profile_id=profile_id, initial=initial)
            if document is None:
                print('survey cancelled; no profile saved')
            else:
                result = import_preferences(database, document, updated_at=now())
                print(
                    f'saved profile {result.profile_id} revision {result.profile_revision}; database: {database.resolve()}'
                )
        return 0
    except (ConfigError, RevisionConflict) as error:
        print(f'applicant survey: {error}', file=sys.stderr)
        return 2
    except ProfileNotFound as error:
        print(f'applicant survey: {error}', file=sys.stderr)
        return 4
