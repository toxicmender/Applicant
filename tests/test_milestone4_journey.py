"""Full subprocess M4 contract and the per-module coverage gate."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.check_milestone_coverage import (
    MODULES,
    check,
    main,
    protocol_exclusions,
)
from tools.check_milestone_coverage import (
    ROOT as COVERAGE_ROOT,
)

ROOT = Path(__file__).resolve().parent.parent


class MilestoneJourneyTest(unittest.TestCase):
    def test_subprocess_journey_matches_golden_inputs_without_host_state(self):
        environment = dict(os.environ)
        environment.update(
            APPLICANT_CONFIG='must-not-read.toml',
            APPLICANT_STORE='files',
            APPLICANT_HOME='must-not-write',
            CRUNCHBASE_API_KEY='must-not-use',
            TRACXN_API_KEY='must-not-use',
            PYTHONIOENCODING='utf-8',
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'smoke_milestone4.py')],
                cwd=directory,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])
        expected = json.loads(
            (ROOT / 'tests' / 'fixtures' / 'milestone4' / 'journey.json').read_text(
                encoding='utf-8'
            )
        )
        self.assertEqual(
            json.loads(result.stdout), {k: v for k, v in expected.items() if k != 'fixture_version'}
        )


class MilestoneCoverageGateTest(unittest.TestCase):
    def report(self):
        return {
            'meta': {'branch_coverage': True},
            'files': {
                'src/applicant/' + name: {
                    'summary': {
                        'num_statements': 90,
                        'covered_lines': 90,
                        'num_branches': 10,
                        'covered_branches': 10,
                    },
                    'excluded_lines': [],
                }
                for name in MODULES
            },
        }

    def test_all_modules_missing_paths_branch_data_and_platform_paths(self):
        report = self.report()
        self.assertEqual(len(MODULES), 28)
        self.assertTrue(check(report)['success'])
        windows = copy.deepcopy(report)
        windows['files'] = {k.replace('/', '\\'): v for k, v in windows['files'].items()}
        self.assertEqual(check(windows), check(report))
        entry = next(iter(report['files']))
        report['files'].pop(entry)
        self.assertFalse(check(report)['success'])
        report['meta']['branch_coverage'] = False
        with self.assertRaises(ValueError):
            check(report)
        report = self.report()
        report['files'][entry.replace('/', '\\')] = report['files'][entry]
        with self.assertRaises(ValueError):
            check(report)

    def test_exact_floor_exclusions_invalid_counts_and_no_rounding_pass(self):
        source = (COVERAGE_ROOT / 'src' / 'applicant' / 'domain' / 'benchmarks.py').read_text(
            encoding='utf-8'
        )
        approved = protocol_exclusions(source)
        self.assertTrue(approved)
        stub = 'class BenchmarkProvider(Protocol):\n def fetch(self): ...\n'
        self.assertIn(3, protocol_exclusions(stub))
        self.assertNotIn(3, protocol_exclusions(stub + 'execute()\n'))
        report = self.report()
        report['files']['src/applicant/domain/benchmarks.py']['excluded_lines'] = sorted(approved)
        self.assertTrue(check(report)['success'])
        report['files']['src/applicant/domain/benchmarks.py']['excluded_lines'] = [1]
        self.assertFalse(check(report)['success'])
        for source in (
            'class Other: pass',
            'class BenchmarkProvider: pass',
            '@decorator\nclass BenchmarkProvider(Protocol): pass',
            'class BenchmarkProvider(Protocol):\n def fetch(self): return 1',
            'class BenchmarkProvider(Protocol):\n def fetch(self, x=call()): ...',
        ):
            self.assertEqual(protocol_exclusions(source), set())
        for changes, success in (
            ({'covered_lines': 80}, True),
            ({'covered_lines': 79}, False),
            (
                {
                    'num_statements': 99990,
                    'covered_lines': 89989,
                    'num_branches': 10,
                    'covered_branches': 10,
                },
                False,
            ),
        ):
            report = self.report()
            first = next(iter(report['files'].values()))
            first['summary'].update(changes)
            self.assertEqual(check(report)['success'], success)
        report = self.report()
        first = next(iter(report['files'].values()))
        first['excluded_lines'] = [1]
        self.assertFalse(check(report)['success'])
        for patch in (
            {'covered_lines': True},
            {'num_branches': -1},
            {'covered_lines': 91},
            {'covered_branches': 11},
            {'num_statements': 0, 'covered_lines': 0, 'num_branches': 0, 'covered_branches': 0},
        ):
            report = self.report()
            next(iter(report['files'].values()))['summary'].update(patch)
            with self.assertRaises(ValueError):
                check(report)

    def test_cli_returns_failure_for_unreadable_or_malformed_coverage(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()) as output:
            path = Path(directory) / 'coverage.json'
            self.assertEqual(main([str(path)]), 1)
            for value in ('{bad', '[]', '{}'):
                path.write_text(value, encoding='utf-8')
                self.assertEqual(main([str(path)]), 1)
            insufficient = self.report()
            next(iter(insufficient['files'].values()))['summary']['covered_lines'] = 0
            path.write_text(json.dumps(insufficient), encoding='utf-8')
            self.assertEqual(main([str(path)]), 1)
            self.assertFalse(json.loads(output.getvalue().splitlines()[-1])['success'])
            path.write_text(json.dumps(self.report()), encoding='utf-8')
            self.assertEqual(main([str(path)]), 0)
            self.assertTrue(json.loads(output.getvalue().splitlines()[-1])['success'])
