import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from applicant.domain.canonical import canonical_json
from applicant.domain.company_time import event_bounds, event_phase
from applicant.domain.research import CompanyEventV2, ResearchImport, parse_import
from applicant.domain.research_reports import company_report
from applicant.services.research import import_research, read_research
from tests.research_fixtures import NOW, bundle


class ResearchRepairsTest(unittest.TestCase):
    def test_versioned_persistence_keeps_v1_bytes_and_import_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'db'
            original = bundle()
            import_research(database, original)
            before = canonical_json(read_research(database)['events'])
            value = original.events[0].model_dump(mode='json')
            value.update(
                schema_version=2,
                event_id='precise-event',
                effective_date=None,
                effective_at='2026-06-01T12:00:00+05:30',
                supersedes=value['event_id'],
            )
            import_research(database, {'schema_version': 2, 'events': [value]})
            self.assertEqual(import_research(database, original)['inserted'], 0)
            events = read_research(database)['events']
            self.assertEqual(canonical_json([e for e in events if e.schema_version == 1]), before)
            self.assertEqual(
                next(e for e in events if e.schema_version == 2).effective_at, value['effective_at']
            )

    def test_exact_event_boundary_and_version_roundtrip(self):
        old = bundle().events[0].model_dump(mode='json')
        value = {
            **old,
            'schema_version': 2,
            'effective_date': None,
            'effective_at': '2026-06-01T12:00:00+05:30',
        }
        event = CompanyEventV2.model_validate(value)
        start, end = event_bounds(event)
        self.assertEqual(start, end)
        self.assertEqual(event_phase(event, start, NOW), 'post-event contextual')
        self.assertEqual(event.effective_at, value['effective_at'])
        with self.assertRaises(ValueError):
            CompanyEventV2.model_validate({**value, 'effective_at': '2026-06-01T12:00:00'})
        imported = parse_import({'schema_version': 2, 'events': [value]})
        self.assertEqual(imported.events[0], event)
        self.assertIsInstance(parse_import(bundle()), ResearchImport)

    def test_successor_does_not_inherit_current_policy(self):
        value = bundle().model_dump(mode='json')
        value['events'][0]['event_type'] = 'merger'
        parsed = parse_import(value)
        data = {
            key: list(getattr(parsed, key))
            for key in (
                'companies',
                'names',
                'identifiers',
                'events',
                'observations',
                'parties',
                'evidence',
            )
        }
        report = company_report(data, {'entity_ids': ['parent']}, created_at=NOW)
        self.assertFalse(next(s for s in report['sections'] if s['id'] == 'policy')['rows'])
        history = next(s for s in report['sections'] if s['id'] == 'inherited-context')['rows']
        self.assertTrue(any('Post-event contextual' in row[1] for row in history))

    def test_default_date_uses_selected_timezone(self):
        data = {
            key: list(getattr(bundle(), key))
            for key in (
                'companies',
                'names',
                'identifiers',
                'events',
                'observations',
                'parties',
                'evidence',
            )
        }
        stamp = datetime(2026, 11, 10, 22, tzinfo=timezone.utc)
        implicit = company_report(
            data, {'entity_ids': ['startup'], 'timezone': 'Asia/Kolkata'}, created_at=stamp
        )
        explicit = company_report(
            data,
            {'entity_ids': ['startup'], 'timezone': 'Asia/Kolkata', 'as_of': '2026-11-11'},
            created_at=stamp,
        )
        self.assertEqual(implicit['sections'], explicit['sections'])
        self.assertEqual(implicit['manifest']['cutoff'], explicit['manifest']['cutoff'])
