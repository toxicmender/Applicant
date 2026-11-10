import unittest
from datetime import timedelta

from applicant.domain.research import ResearchImport
from applicant.domain.research_analytics import analytics
from tests.research_fixtures import NOW, bundle


class AnalyticsBranchesTest(unittest.TestCase):
    def analyze(self, value=None, **options):
        parsed = ResearchImport.model_validate(value or bundle())
        data = {
            k: list(getattr(parsed, k))
            for k in (
                'companies',
                'names',
                'identifiers',
                'events',
                'observations',
                'parties',
                'evidence',
            )
        }
        return analytics(
            data,
            as_of=options.pop('as_of', '2026-11-10'),
            applicant_timezone='Asia/Kolkata',
            created_at=NOW,
            **options,
        )

    def test_validation_and_filters(self):
        for options in (
            {'group_by': 'bad'},
            {'window_days': 0},
            {'window_days': 3661},
            {'as_of': '2026-11-11'},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.analyze(**options)
        for options in ({'origin': 'agency'}, {'role': 'doctor'}, {'geography': 'France'}):
            self.assertEqual(self.analyze(**options)['rows'], [])
        for status in ('closed', 'unknown'):
            value = bundle().model_dump(mode='json')
            for observation in value['observations']:
                observation['status'] = status
            self.assertFalse(self.analyze(value)['rows'])
        value = bundle().model_dump(mode='json')
        value['observations'][0]['employer_requisition_url'] = 'https://employer.example/one'
        value['observations'][1]['employer_requisition_url'] = 'https://employer.example/one'
        value['observations'][1]['employer_entity_id'] = 'parent'
        self.assertEqual(self.analyze(value)['rows'][0]['observed_openings'], 5)

    def test_salary_exclusions_and_stack_requirements(self):
        for requirement in ('preferred', 'mentioned'):
            value = bundle().model_dump(mode='json')
            for evidence in value['evidence']:
                if evidence['payload']['kind'] == 'technology':
                    evidence['payload']['requirement'] = requirement
            self.assertEqual(self.analyze(value, group_by='stack')['rows'], [])
        for mode in ('missing', 'conflict', 'fx'):
            value = bundle().model_dump(mode='json')
            if mode == 'missing':
                value['evidence'] = [
                    e for e in value['evidence'] if e['payload']['kind'] != 'compensation'
                ]
            elif mode == 'conflict':
                value['evidence'][1]['payload']['minimum'] = '1'
            result = self.analyze(value, currency='EUR' if mode == 'fx' else None)
            reasons = {m['compensation_reason'] for m in result['members']}
            self.assertIn(
                {
                    'missing': 'missing_comparable_salary',
                    'conflict': 'conflicting_salary_observations',
                    'fx': 'missing_fx',
                }[mode],
                reasons,
            )

    def test_anchored_fx_and_future_rejection(self):
        fx = {
            'snapshot_id': 'test',
            'base_currency': 'USD',
            'source': 'fixture',
            'captured_at': NOW.isoformat(),
            'retrieved_at': NOW.isoformat(),
            'effective_date': '2026-11-09',
            'provenance': 'live',
            'rates': {'USD': '1', 'EUR': '0.5'},
        }
        result = self.analyze(currency='EUR', fx=fx)
        self.assertEqual(
            result['rows'][0]['compensation'][0]['median_annual_gross_base'], '56250.00'
        )
        self.assertEqual(result['fx_age_days'], 1)
        self.assertEqual(
            self.analyze(currency='GBP', fx=fx)['members'][0]['compensation_reason'], 'missing_fx'
        )
        future = {
            **fx,
            'effective_date': '2026-11-11',
            'captured_at': (NOW + timedelta(days=1)).isoformat(),
            'retrieved_at': (NOW + timedelta(days=1)).isoformat(),
        }
        with self.assertRaises(ValueError):
            self.analyze(fx=future)

    def test_input_order_does_not_change_metrics(self):
        value = bundle().model_dump(mode='json')
        first = self.analyze(value)
        for key in ('observations', 'evidence', 'parties'):
            value[key].reverse()
        self.assertEqual(first['rows'], self.analyze(value)['rows'])
