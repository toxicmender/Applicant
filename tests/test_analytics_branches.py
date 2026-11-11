import unittest
from datetime import timedelta
from decimal import ROUND_UP, Inexact, localcontext
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from applicant.domain.research import ResearchImport
from applicant.domain.research_analytics import analytics, money_text
from applicant.domain.research_reports import render_markdown, snapshot_report
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

    @settings(max_examples=20)
    @given(st.randoms())
    def test_input_order_does_not_change_snapshot(self, randomizer):
        value = bundle().model_dump(mode='json')
        first = self.analyze(value)
        for key in ('observations', 'evidence', 'parties'):
            randomizer.shuffle(value[key])
        self.assertEqual(first, self.analyze(value))

    def test_decimal_context_and_half_even(self):
        expected = self.analyze()
        with localcontext() as caller:
            caller.prec, caller.rounding = 3, ROUND_UP
            caller.traps[Inexact] = True
            self.assertEqual(expected, self.analyze())
            self.assertEqual((caller.prec, caller.rounding), (3, ROUND_UP))
            self.assertTrue(caller.traps[Inexact])
        self.assertEqual(money_text(Fraction(1005, 1000)), '1.00')
        self.assertEqual(money_text(Fraction(1015, 1000)), '1.02')

    def test_render_discloses_policy_without_rewriting_snapshot(self):
        expected = self.analyze()
        for policy in ('research-analytics-1', 'research-analytics-2', 'future', None):
            with self.subTest(policy=policy):
                value = {**expected, 'policy_version': policy}
                if policy is None:
                    value.pop('policy_version')
                original = value.copy()
                report = snapshot_report(value)
                self.assertEqual(value, original)
                self.assertEqual(report['manifest']['policy_version'], policy)
                self.assertIn(policy or 'Metric policy missing', render_markdown(report))
                self.assertEqual(
                    'policy_diagnostic' in report['manifest'], policy in ('future', None)
                )
