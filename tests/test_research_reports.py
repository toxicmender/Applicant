"""Offline report documents consolidate jobs and retain original research context."""

import unittest

from applicant.domain.research_reports import company_report, parse_markdown, render_markdown
from tests.research_fixtures import NOW, bundle


class ReportDocumentsTest(unittest.TestCase):
    def test_jobs_are_consolidated_and_historical_evidence_is_visible(self):
        fixture = bundle()
        data = {
            key: list(getattr(fixture, key))
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
        report = company_report(
            data, {'entity_ids': ['startup'], 'timezone': 'Asia/Kolkata'}, created_at=NOW
        )
        self.assertEqual(len(next(x for x in report['sections'] if x['id'] == 'jobs')['rows']), 6)
        self.assertEqual(len(report['manifest']['job_version_ids']), 12)
        self.assertEqual(parse_markdown(render_markdown(report)), report)
        historical = next(x for x in report['sections'] if x['id'] == 'inherited-context')
        self.assertIn('Café Startup', historical['rows'][0][1])
        self.assertIn('2025-01-01', historical['rows'][0][1])
