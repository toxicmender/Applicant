"""Typed CLI surface for acquisition, immutable analysis and report exports."""

import os
import tempfile
from pathlib import Path

from pydantic import Field, ValidationError

from ..domain.canonical import canonical_json
from ..domain.contracts import now
from ..domain.research import ResearchImport
from ..domain.research_analytics import analytics
from ..domain.research_reports import (
    ReportDocument,
    company_report,
    parse_markdown,
    render_markdown,
    snapshot_report,
)
from ..errors import ConfigError
from ..infra.ranking_documents import read_ranking_document
from ..permissions import make_private
from ..services.research import (
    import_research,
    read_research,
    read_snapshot,
    save_snapshot,
    template,
)
from ..services.research_feed import collect_feed, feed_from_imports
from .database import database_path, export_path
from .dispatch import DatabaseRequest, actions, bind


class ResearchRequest(DatabaseRequest):
    operation: str
    kind: str = 'compensation'
    output: str | None = None
    export_schema: str | None = None
    from_file: str | None = None
    selection: str | None = None
    query: str | None = None
    from_imports: bool = False
    dry_run: bool = False
    feed: str | None = None
    snapshot: str | None = None
    format: str = 'json'
    pdf: str | None = None
    missing_glyph: str = 'escape'
    section: str | None = None
    summary: bool = False
    limit: int = Field(default=25, ge=1, le=10000)
    offset: int = Field(default=0, ge=0)
    as_of: str | None = None
    timezone: str | None = None
    group_by: str = 'technology'
    window_days: int = Field(default=30, ge=1, le=3660)
    currency: str | None = None
    fx_snapshot: str | None = None
    origin: str | None = None
    role: str | None = None
    geography: str | None = None
    require_all_sources: bool = False
    entity_id: str | None = None


def add_parser(add):
    specifications = {
        'research': (
            'template',
            'import',
            'report',
            'capture-profile',
            'bridge-company',
            'backfill-companies',
        ),
        'feed': ('collect', 'refresh', 'show'),
        'analytics': ('technologies', 'show'),
        'report': ('render',),
    }
    for family, operations in specifications.items():
        group = actions(add(family, help=f'{family} research workflows'))
        for operation in operations:
            p = group(operation, help=f'{operation} {family} data')
            op = family + '.' + operation
            p.set_defaults(operation=op)
            p.add_argument('--database')
            p.add_argument('--json', action='store_true')
            p.add_argument('--output', '--export', dest='output')
            if op == 'research.template':
                p.add_argument(
                    '--kind',
                    choices=(
                        'compensation',
                        'culture',
                        'policy',
                        'technology',
                        'job-party',
                        'corporate-event',
                    ),
                    default='compensation',
                )
                p.add_argument('--export-schema')
            elif op in ('research.import', 'report.render', 'research.capture-profile'):
                p.add_argument('--from-file', required=True)
                if op == 'research.import':
                    p.add_argument('--dry-run', action='store_true')
            elif op == 'research.bridge-company':
                p.add_argument('--snapshot', required=True)
                p.add_argument('--entity-id', required=True)
            elif op == 'research.report':
                p.add_argument('--selection', required=True)
                p.add_argument('--section')
                p.add_argument('--summary', action='store_true')
            elif op == 'feed.collect':
                choice = p.add_mutually_exclusive_group(required=True)
                choice.add_argument('--query')
                choice.add_argument('--from-imports', action='store_true')
                p.add_argument('--require-all-sources', action='store_true')
            elif family == 'feed':
                p.add_argument('--feed', required=True)
            elif op == 'analytics.show':
                p.add_argument('--snapshot', required=True)
            elif op == 'analytics.technologies':
                p.add_argument('--group-by', choices=('technology', 'stack'), default='technology')
                p.add_argument('--window-days', type=int, default=30)
                p.add_argument('--currency')
                p.add_argument('--fx-snapshot')
                p.add_argument('--origin', choices=('direct', 'agency', 'unknown'))
                p.add_argument('--role')
                p.add_argument('--geography')
            if op in ('research.report', 'analytics.technologies'):
                p.add_argument('--as-of')
                p.add_argument('--timezone')
            if op not in ('research.import', 'research.template'):
                p.add_argument('--format', choices=('json', 'markdown'), default='json')
                p.add_argument('--pdf')
                p.add_argument('--missing-glyph', choices=('escape', 'error'), default='escape')
                p.add_argument('--limit', type=int, default=25)
                p.add_argument('--offset', type=int, default=0)
            bind(p, ResearchRequest, run)


def atomic_text(target, text):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + target.name, dir=target.parent)
    try:
        make_private(temporary)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def run(args, settings):
    try:
        return execute(args, settings)
    except ValidationError as error:
        details = '; '.join(
            '.'.join(map(str, item['loc'])) + ': ' + item['msg']
            for item in error.errors(include_input=False, include_context=False)
        )
        raise ConfigError('invalid research input: ' + details) from None
    except ValueError as error:
        raise ConfigError(str(error)) from error


def execute(args, settings):
    database = database_path(settings, args.database)
    output = export_path(settings, database, args.output)
    pdf = export_path(settings, database, args.pdf)
    schema_path = export_path(settings, database, args.export_schema)
    targets = [x for x in (output, pdf, schema_path) if x]
    if len(targets) != len(set(targets)):
        raise ConfigError('export destinations must be distinct')
    inputs = {
        Path(settings.path(x)).resolve()
        for x in (args.from_file, args.selection, args.query, args.fx_snapshot)
        if x
    }
    if any(target in inputs for target in targets):
        raise ConfigError('exports must not replace source inputs')

    def read(path):
        return read_ranking_document(Path(settings.path(path)))

    op, code = args.operation, 0
    if op == 'research.template':
        result = template(args.kind)
        if schema_path:
            atomic_text(schema_path, canonical_json(ResearchImport.model_json_schema()) + '\n')
    elif op == 'research.import':
        result = import_research(database, read(args.from_file), dry_run=args.dry_run)
    elif op == 'research.capture-profile':
        from ..services.company_capture import capture_profile

        result = capture_profile(database, read(args.from_file))
        code = int(
            result.get('status') in ('cooldown', 'needs_setup', 'adapter_degraded', 'leased')
        )
    elif op == 'research.bridge-company':
        from ..services.company_capture import bridge_company_snapshot

        result = bridge_company_snapshot(database, args.snapshot, args.entity_id)
    elif op == 'research.backfill-companies':
        from ..services.company_capture import backfill_verified_companies

        result = backfill_verified_companies(database)
    elif op == 'research.report':
        selection = read(args.selection)
        if args.section:
            selection['sections'] = args.section.split(',')
        if args.summary:
            selection['summary'] = True
        if args.as_of:
            selection['as_of'] = args.as_of
        if args.timezone:
            selection['timezone'] = args.timezone
        selection.setdefault('timezone', settings.timezone)
        result = save_snapshot(
            database, company_report(read_research(database), selection, created_at=now())
        )
    elif op == 'feed.collect':
        result = (
            feed_from_imports(database)
            if args.from_imports
            else collect_feed(database, read(args.query))
        )
        failures = (
            any(x['status'] != 'success' for x in result['outcomes']) and not args.from_imports
        )
        code = int(
            not result['job_version_ids']
            or (
                failures
                and (args.require_all_sources or result['query'].get('require_all_sources'))
            )
        )
    elif op == 'feed.refresh':
        previous = read_snapshot(database, args.feed)
        if previous['kind'] != 'research_feed':
            raise ConfigError('feed reference is not a feed')
        result = (
            feed_from_imports(database)
            if previous['query']['providers'] == ['import']
            else collect_feed(database, previous['query'], parent=args.feed)
        )
        code = int(not result['job_version_ids'])
    elif op in ('feed.show', 'analytics.show'):
        result = read_snapshot(database, args.feed or args.snapshot)
        expected = 'research_feed' if op == 'feed.show' else 'technology_analytics'
        if result['kind'] != expected:
            raise ConfigError('snapshot has the wrong kind')
    elif op == 'analytics.technologies':
        stamp = now()
        result = save_snapshot(
            database,
            analytics(
                read_research(database),
                as_of=args.as_of or stamp.date().isoformat(),
                applicant_timezone=args.timezone or settings.timezone,
                created_at=stamp,
                group_by=args.group_by,
                window_days=args.window_days,
                currency=args.currency,
                fx=read(args.fx_snapshot) if args.fx_snapshot else None,
                origin=args.origin,
                role=args.role,
                geography=args.geography,
            ),
        )
    elif op == 'report.render':
        source = Path(settings.path(args.from_file))
        result = (
            parse_markdown(source.read_text(encoding='utf-8'))
            if source.suffix.lower() == '.md'
            else read(args.from_file)
        )
        if result.get('kind') != 'applicant_report':
            raise ConfigError('expected an Applicant report document')
        ReportDocument.model_validate(result)
    else:
        raise ConfigError('unknown research operation')
    document = None
    if pdf or args.format == 'markdown':
        document = snapshot_report(result, limit=args.limit, offset=args.offset)
    if pdf:
        from ..infra.research_pdf import render_pdf

        manifest_path = pdf.with_suffix(pdf.suffix + '.manifest.json')
        if (
            manifest_path in inputs
            or manifest_path in targets
            or manifest_path
            in {
                database.resolve(),
                Path(str(database) + '-wal').resolve(),
                Path(str(database) + '-shm').resolve(),
            }
        ):
            raise ConfigError('PDF manifest would replace an input or export')
        manifest = render_pdf(document, pdf, missing_glyph=args.missing_glyph)
        atomic_text(manifest_path, canonical_json(manifest) + '\n')
    text = render_markdown(document) if args.format == 'markdown' else canonical_json(result) + '\n'
    if output:
        atomic_text(output, text)
        print(
            canonical_json(
                {
                    'output': str(output),
                    'snapshot_id': result.get('snapshot_id'),
                    'pdf': str(pdf) if pdf else None,
                }
            )
        )
    else:
        print(text.rstrip())
    return code
