import unittest

from tools.check_research_coverage import MODULES, check


class ResearchCoverageGateTest(unittest.TestCase):
    def report(self, lines=90, branches=90):
        return {
            'meta': {'branch_coverage': True},
            'files': {
                'src\\applicant\\' + module.replace('/', '\\'): {
                    'summary': {
                        'num_statements': 100,
                        'covered_lines': lines,
                        'num_branches': 100,
                        'covered_branches': branches,
                    },
                    'excluded_lines': [],
                }
                for module in MODULES
            },
        }

    def test_independent_thresholds(self):
        self.assertTrue(check(self.report())['success'])
        self.assertFalse(check(self.report(89, 100))['success'])
        self.assertFalse(check(self.report(100, 89))['success'])

    def test_invalid_and_missing_data(self):
        report = self.report()
        report['files'].pop(next(iter(report['files'])))
        self.assertFalse(check(report)['success'])
        for report in ({}, self.report(-1), self.report(101)):
            with self.assertRaises(ValueError):
                check(report)
