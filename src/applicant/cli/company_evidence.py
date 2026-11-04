"""Explicit verified company capture, strict imports and offline enrichment."""

import json
import sys
from pathlib import Path

from ..domain.canonical import canonical_json
from ..domain.company_evidence import CompanyAssociation, CompanyEnrichment, CompanyEvidenceImport
from ..errors import ConfigError, StoreError
from ..files import write_document
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.company_evidence import CompanyEvidenceNotFound
from ..services.company_evidence import (
    capture_company_evidence,
    enrich_company_evidence,
    import_company_evidence,
    show_company_evidence,
)
from ..settings import Settings
from .dispatch import Request
from .feedback import database_path


class ImportEvidence(Request):
    from_file: str
    dry_run: bool = False

    def document(self, settings: Settings) -> CompanyEvidenceImport:
        data = read_ranking_document(Path(settings.path(self.from_file)), decimal=True)
        return CompanyEvidenceImport.model_validate(data)

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        return import_company_evidence(database, self.document(settings), dry_run=self.dry_run)


class Associate(ImportEvidence):
    def document(self, settings: Settings) -> CompanyEvidenceImport:
        data = read_ranking_document(Path(settings.path(self.from_file)), decimal=True)
        return CompanyEvidenceImport(associations=(CompanyAssociation.model_validate(data),))


class Enrich(Request):
    from_file: str
    export: str

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        data = read_ranking_document(Path(settings.path(self.from_file)), decimal=True)
        return enrich_company_evidence(
            database, CompanyEnrichment.model_validate(data)
        ).model_dump()


class ShowEvidence(Request):
    association: list[str]
    snapshot: list[str]

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        return show_company_evidence(
            database, association_ids=tuple(self.association), snapshot_ids=tuple(self.snapshot)
        )


def add_parser(add):
    command = add('company-evidence', help='associate, capture and enrich exact company evidence')
    command.add_argument(
        'action', choices=('associate', 'import', 'capture', 'refresh', 'show', 'enrich')
    )
    command.add_argument('--database')
    command.add_argument('--from-file')
    command.add_argument('--association', action='append', default=[])
    command.add_argument('--snapshot', action='append', default=[])
    command.add_argument('--dry-run', action='store_true')
    command.add_argument('--export')
    command.add_argument('--json', action='store_true')
    command.add_argument('--crunchbase-key')
    command.add_argument('--tracxn-key')
    return command


def run(args):
    try:
        database = database_path(args)
        target = Path(args.settings.path(args.export)).resolve() if args.export else None
        protected = {
            database.resolve(),
            *(Path(str(database) + s).resolve() for s in ('-wal', '-shm')),
        }
        if target in protected:
            raise ConfigError('export must not replace database or sidecars')
        if args.action in ('associate', 'import', 'enrich'):
            if not args.from_file or args.association or args.snapshot:
                raise ConfigError(
                    'associate/import/enrich require --from-file without association/snapshot flags'
                )
            if args.action == 'enrich' and (args.dry_run or target is None):
                raise ConfigError('enrich requires --export without --dry-run')
            if args.action == 'enrich':
                request = Enrich(from_file=args.from_file, export=args.export)
            elif args.action == 'associate':
                request = Associate(from_file=args.from_file, dry_run=args.dry_run)
            else:
                request = ImportEvidence(from_file=args.from_file, dry_run=args.dry_run)
            report = request.report(database, args.settings)
        else:
            if (
                args.from_file
                or args.dry_run
                or not (args.association or args.snapshot)
                or any(not k.strip() for k in (*args.association, *args.snapshot))
            ):
                raise ConfigError(
                    'show/capture/refresh require explicit IDs without --from-file/--dry-run'
                )
            if args.action == 'show':
                report = ShowEvidence(association=args.association, snapshot=args.snapshot).report(
                    database, args.settings
                )
            else:
                if args.snapshot:
                    raise ConfigError('capture/refresh require --association without --snapshot')
                report = capture_company_evidence(
                    database, tuple(args.association), settings=args.settings
                )
        if target is not None:
            try:
                write_document(
                    str(target), json.loads(canonical_json(report)), trailing_newline=True
                )
            except OSError as error:
                raise StoreError(f'could not export company evidence: {error}') from error
        print(canonical_json(report))
        return 1 if report.get('success') is False else 0
    except (ValueError, ConfigError) as error:
        print(f'applicant company-evidence: {error}', file=sys.stderr)
        return 2
    except CompanyEvidenceNotFound as error:
        print(f'applicant company-evidence: {error}', file=sys.stderr)
        return 4
