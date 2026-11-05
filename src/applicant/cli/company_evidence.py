"""Explicit verified company capture, strict imports and offline enrichment."""

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path

from pydantic import Field, model_validator

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
from .database import database_path, export_path
from .dispatch import AddParser, DatabaseRequest, Identifier, actions, bind


class EvidenceRequest(DatabaseRequest):
    export: str | None = None


class ImportEvidence(EvidenceRequest):
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


class Enrich(DatabaseRequest):
    from_file: str
    export: str

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        data = read_ranking_document(Path(settings.path(self.from_file)), decimal=True)
        return enrich_company_evidence(
            database, CompanyEnrichment.model_validate(data)
        ).model_dump()


class ShowEvidence(EvidenceRequest):
    association: list[Identifier] = Field(default_factory=list)
    snapshot: list[Identifier] = Field(default_factory=list)

    @model_validator(mode='after')
    def require_selection(self):
        if not self.association and not self.snapshot:
            raise ValueError('supply --association or --snapshot')
        return self

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        return show_company_evidence(
            database, association_ids=tuple(self.association), snapshot_ids=tuple(self.snapshot)
        )


class Capture(EvidenceRequest):
    association: list[Identifier]

    def report(self, database: Path, settings: Settings) -> dict[str, object]:
        return capture_company_evidence(database, tuple(self.association), settings=settings)


Evidence = ImportEvidence | ShowEvidence | Enrich | Capture


def add_parser(add: AddParser, handler: Callable[[Evidence, Settings], int]) -> None:
    action = actions(
        add('company-evidence', help='associate, capture and enrich exact company evidence')
    )

    def leaf(name: str, help: str) -> argparse.ArgumentParser:
        command = action(name, help=help)
        command.add_argument('--database')
        command.add_argument('--export', required=name == 'enrich')
        command.add_argument('--json', action='store_true')
        return command

    for name, model in (('associate', Associate), ('import', ImportEvidence)):
        command = leaf(
            name,
            'import a company association'
            if name == 'associate'
            else 'import an evidence document',
        )
        command.add_argument('--from-file', required=True)
        command.add_argument('--dry-run', action='store_true')
        bind(command, model, handler)
    command = leaf('enrich', 'export job versions enriched with selected company evidence')
    command.add_argument('--from-file', required=True)
    bind(command, Enrich, handler)
    command = leaf('show', 'read selected company evidence offline')
    command.add_argument('--association', action='append', default=[])
    command.add_argument('--snapshot', action='append', default=[])
    bind(command, ShowEvidence, handler)
    for name in ('capture', 'refresh'):
        command = leaf(name, 'capture verified evidence for selected associations')
        command.add_argument('--association', action='append', required=True)
        command.add_argument('--crunchbase-key')
        command.add_argument('--tracxn-key')
        bind(command, Capture, handler)


def run(args: Evidence, settings: Settings) -> int:
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
                raise StoreError(f'could not export company evidence: {error}') from error
        print(canonical_json(report))
        return 1 if report.get('success') is False else 0
    except (ValueError, ConfigError) as error:
        print(f'applicant company-evidence: {error}', file=sys.stderr)
        return 2
    except CompanyEvidenceNotFound as error:
        print(f'applicant company-evidence: {error}', file=sys.stderr)
        return 4
