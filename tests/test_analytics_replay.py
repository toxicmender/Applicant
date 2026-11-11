import tempfile
import unittest
from pathlib import Path

from applicant.domain.research_analytics import analytics
from applicant.domain.research_reports import parse_markdown, render_markdown, snapshot_report
from applicant.infra.store.sqlite import Store
from applicant.services.research import import_research, read_research, read_snapshot, save_snapshot
from tests.research_fixtures import NOW, bundle


class AnalyticsReplayTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.database = Path(folder.name) / 'research.db'
        self.bundle = bundle()

    def test_legacy_analytics_snapshot_payload_and_hash_survive_export(self):
        import_research(self.database, self.bundle)
        value = analytics(
            read_research(self.database),
            as_of='2026-11-10',
            applicant_timezone='Asia/Kolkata',
            created_at=NOW,
        )
        value['policy_version'] = 'research-analytics-1'
        value['rows'][0]['compensation'][0]['median_annual_gross_base'] = '112500'
        snapshot_id = save_snapshot(self.database, value)['snapshot_id']
        with Store.open_readonly(self.database) as store:
            before = store.db.execute(
                'SELECT * FROM research_snapshots WHERE id=?', (snapshot_id,)
            ).fetchone()
        legacy = read_snapshot(self.database, snapshot_id)
        markdown = render_markdown(snapshot_report(legacy))
        self.assertIn('Metric policy: research-analytics-1', markdown)
        self.assertEqual(parse_markdown(markdown)['manifest']['snapshot'], legacy)
        with Store.open_readonly(self.database) as store:
            after = store.db.execute(
                'SELECT * FROM research_snapshots WHERE id=?', (snapshot_id,)
            ).fetchone()
        self.assertEqual(before, after)
