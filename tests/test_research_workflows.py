"""Research imports, snapshots and human-readable reports preserve history."""

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from hypothesis import given
from hypothesis import strategies as st

from applicant.cli import main
from applicant.domain.canonical import canonical_json
from applicant.domain.research import ResearchImport, Schedule
from applicant.domain.research_analytics import analytics
from applicant.domain.research_identity import match_jobs, requisition_url
from applicant.domain.research_reports import (
    company_report,
    parse_markdown,
    render_markdown,
)
from applicant.domain.research_time import convert_schedule, localize, zone
from applicant.errors import ConfigError, StoreError
from applicant.infra.research_pdf import render_pdf
from applicant.infra.store.sqlite import Store
from applicant.services.research import (
    import_research,
    read_research,
    read_snapshot,
    save_snapshot,
    template,
)
from tests.research_fixtures import NOW, bundle


class ResearchWorkflowTest(unittest.TestCase):
    def test_verified_legacy_snapshot_bridge_is_retry_safe(self):
        from applicant.domain.company_evidence import CompanyEvidenceImport
        from applicant.infra.store.job_versions import JobVersionRepository
        from applicant.infra.store.ranking_common import write_transaction
        from applicant.services.company_capture import (
            backfill_verified_companies,
            bridge_company_snapshot,
        )
        from applicant.services.company_evidence import import_company_evidence
        from tests.test_company_evidence import association, snapshot
        from tests.test_ranking_engine import version

        job = version('legacy-bridge')
        with Store(self.database) as store, write_transaction(store.db):
            JobVersionRepository(store.db).put(job)
        associated = association(job)
        captured = snapshot(associated)
        import_company_evidence(
            self.database, CompanyEvidenceImport(associations=(associated,), snapshots=(captured,))
        )
        self.assertEqual(backfill_verified_companies(self.database)['inserted'], 2)
        self.assertEqual(backfill_verified_companies(self.database)['inserted'], 0)
        entity = read_research(self.database)['companies'][0].entity_id
        self.assertEqual(
            bridge_company_snapshot(self.database, captured.snapshot_id, entity)['inserted'], 1
        )
        self.assertEqual(
            bridge_company_snapshot(self.database, captured.snapshot_id, entity)['inserted'], 0
        )
        evidence = read_research(self.database)['evidence'][0]
        self.assertEqual(evidence.responses, captured.responses)

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = self.root / 'applicant.db'
        self.bundle = bundle()

    def test_import_is_atomic_retry_safe_and_dry_run_does_not_create_database(self):
        result = import_research(self.database, self.bundle, dry_run=True)
        self.assertGreater(result['inserted'], 0)
        self.assertFalse(self.database.exists())
        import_research(self.database, self.bundle)
        self.assertEqual(import_research(self.database, self.bundle)['inserted'], 0)
        invalid = self.bundle.model_dump(mode='json')
        invalid['companies'][0]['name'] = 'Conflicting identity'
        with self.assertRaises(StoreError):
            import_research(self.database, invalid)
        self.assertEqual(read_research(self.database)['companies'][1].name, 'Café Startup')
        with Store(self.database) as store:
            for statement in (
                'DELETE FROM company_entities',
                "UPDATE research_job_versions SET payload='{}'",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute(statement)
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM job_technology_facts').fetchone()[0], 12
            )
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM company_relationships').fetchone()[0], 1
            )

    def test_strong_matches_syndication_and_similar_requisitions(self):
        groups = match_jobs(self.bundle.observations)
        self.assertEqual(len(groups), 6)
        self.assertTrue(all(x['status'] == 'confirmed' for x in groups))
        self.assertTrue(all(not x['independent_corroboration'] for x in groups))
        self.assertTrue(all(not x['possible_match_groups'] for x in groups))
        same_name = self.bundle.observations[0].model_copy(
            update={'job_version_id': 'agency', 'employer_requisition_id': None, 'origin': 'agency'}
        )
        groups = match_jobs([same_name, self.bundle.observations[1]])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(x['status'] == 'possible_match' for x in groups))
        self.assertEqual(
            requisition_url('https://EXAMPLE.com/job?id=42&utm_source=x'),
            'https://example.com/job?id=42',
        )

    def test_analytics_membership_frozen_after_closure_and_backdated_import(self):
        import_research(self.database, self.bundle)
        snapshot = save_snapshot(
            self.database,
            analytics(
                read_research(self.database),
                as_of='2026-11-10',
                applicant_timezone='Asia/Kolkata',
                created_at=NOW,
            ),
        )
        self.assertEqual(len(snapshot['members']), 12)
        self.assertEqual(snapshot['rows'][0]['observed_openings'], 6)
        self.assertEqual(snapshot['rows'][0]['compensation'][0]['sample_size'], 6)
        self.assertEqual(
            snapshot['rows'][0]['compensation'][0]['median_annual_gross_base'], '112500.00'
        )
        before = canonical_json(read_snapshot(self.database, snapshot['snapshot_id']))
        closed = self.bundle.observations[0].model_copy(
            update={
                'job_version_id': 'closed-version',
                'acquired_at': NOW + timedelta(days=1),
                'status': 'closed',
            }
        )
        import_research(self.database, ResearchImport(observations=(closed,)))
        self.assertEqual(
            canonical_json(read_snapshot(self.database, snapshot['snapshot_id'])), before
        )
        with Store.open_readonly(self.database) as store:
            self.assertEqual(
                store.db.execute('SELECT count(*) FROM analytics_members').fetchone()[0], 12
            )
        self.assertEqual(
            analytics(
                read_research(self.database),
                as_of='2026-11-10',
                applicant_timezone='Asia/Kolkata',
                created_at=NOW,
            )['rows'],
            snapshot['rows'],
        )

    def test_report_inherited_context_and_markdown_integrity(self):
        import_research(self.database, self.bundle)
        report = company_report(
            read_research(self.database),
            {'entity_ids': ['startup'], 'timezone': 'Asia/Kolkata', 'summary': True},
            created_at=NOW,
        )
        history = next(x for x in report['sections'] if x['id'] == 'inherited-context')
        self.assertIn('2026-06-01 acquisition by Parent Group', history['rows'][0][1])
        self.assertEqual(next(x for x in report['sections'] if x['id'] == 'culture')['rows'], [])
        self.assertGreaterEqual(len(report['manifest']['conversions'][0]['variants']), 2)
        markdown = render_markdown(report)
        self.assertEqual(parse_markdown(markdown), report)
        with self.assertRaises(ValueError):
            parse_markdown(markdown.replace('Job and Company Research', 'Altered', 1))

    def test_pdf_latin_and_explicit_indic_escapes(self):
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest('install applicant[pdf]')
        import_research(self.database, self.bundle)
        report = company_report(
            read_research(self.database),
            {'entity_ids': ['startup'], 'timezone': 'Asia/Kolkata', 'summary': True},
            created_at=NOW,
        )
        target = self.root / 'report.pdf'
        manifest = render_pdf(report, target)
        text = '\n'.join(page.extract_text() for page in PdfReader(target).pages)
        for value in ('Café', '₹', '€', '£'):
            self.assertIn(value, text)
        # PDF line wrapping may split a long run of adjacent escapes.
        self.assertIn('\\u0cac', text.replace('\n', ''))
        self.assertIn('\\u092c', text.replace('\n', ''))
        self.assertTrue(manifest['substitutions'])
        before = target.read_bytes()
        with self.assertRaises(ConfigError):
            render_pdf(report, target, missing_glyph='error')
        self.assertEqual(target.read_bytes(), before)

    def call(self, *argv):
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(['--data-dir', str(self.root), '--no-log-file', *argv])
        self.assertEqual(result, 0, output.getvalue())
        return output.getvalue()

    def test_public_cli_template_import_feed_analytics_report(self):
        for kind in (
            'compensation',
            'culture',
            'policy',
            'technology',
            'job-party',
            'corporate-event',
        ):
            from applicant.domain.research import parse_import

            parse_import(template(kind))
        self.call(
            'research', 'template', '--output', 'template.json', '--export-schema', 'schema.json'
        )
        (self.root / 'bundle.json').write_text(canonical_json(self.bundle), encoding='utf-8')
        self.call('research', 'import', '--from-file', 'bundle.json', '--dry-run')
        self.call('research', 'import', '--from-file', 'bundle.json')
        feed = json.loads(self.call('feed', 'collect', '--from-imports'))
        self.call(
            'feed',
            'show',
            '--feed',
            feed['snapshot_id'],
            '--format',
            'markdown',
            '--output',
            'feed.md',
        )
        self.call('feed', 'refresh', '--feed', feed['snapshot_id'])
        with patch('applicant.cli.research.now', return_value=NOW):
            result = json.loads(self.call('analytics', 'technologies', '--as-of', '2026-11-10'))
            self.call('analytics', 'show', '--snapshot', result['snapshot_id'])
            (self.root / 'selection.json').write_text(
                '{"entity_ids":["startup"]}', encoding='utf-8'
            )
            self.call(
                'research',
                'report',
                '--selection',
                'selection.json',
                '--summary',
                '--output',
                'report.json',
            )
        self.call(
            'report',
            'render',
            '--from-file',
            'report.json',
            '--format',
            'markdown',
            '--output',
            'report.md',
        )
        self.call('report', 'render', '--from-file', 'report.md', '--output', 'rendered.json')
        self.call('store', 'checkpoint')


class ResearchTimeTest(unittest.TestCase):
    def test_dst_fixed_est_and_overnight(self):
        schedule = Schedule(employer_timezone='America/New_York', start='09:00', end='17:00')
        self.assertIn(
            '19:30', convert_schedule(schedule, 'Asia/Kolkata', date(2026, 1, 10))['local_start']
        )
        self.assertIn(
            '18:30', convert_schedule(schedule, 'Asia/Kolkata', date(2026, 7, 10))['local_start']
        )
        fixed = schedule.model_copy(update={'employer_timezone': 'EST'})
        self.assertIn(
            '19:30', convert_schedule(fixed, 'Asia/Kolkata', date(2026, 7, 10))['local_start']
        )
        with self.assertRaisesRegex(ValueError, 'nonexistent'):
            localize(date(2026, 3, 8), '02:30', 'America/New_York')
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            localize(date(2026, 11, 1), '01:30', 'America/New_York')
        self.assertNotEqual(
            localize(date(2026, 11, 1), '01:30', 'America/New_York', 0).utcoffset(),
            localize(date(2026, 11, 1), '01:30', 'America/New_York', 1).utcoffset(),
        )
        with self.assertRaises(ValueError):
            zone('IST')

    @given(st.dates(min_value=date(2025, 1, 1), max_value=date(2027, 12, 31)))
    def test_noon_roundtrips(self, day):
        value = localize(day, '12:00', 'America/New_York')
        self.assertEqual(
            value, value.astimezone(zone('Asia/Kolkata')).astimezone(zone('America/New_York'))
        )
