"""The aggregate type gate must retain failures while checking every target."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / 'tools' / 'check_types.py'
TARGETS = [('Linux', '3.10'), ('Linux', '3.12'), ('Windows', '3.10'), ('Windows', '3.12')]
CLEAN = {'summary': {'errorCount': 0}, 'generalDiagnostics': []}


class TypeGateTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('check_types', SCRIPT)
        assert spec is not None and spec.loader is not None
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        patch = mock.patch.dict(
            os.environ,
            {
                'GITHUB_OUTPUT': str(self.root / 'outputs'),
                'GITHUB_STEP_SUMMARY': str(self.root / 'summary'),
            },
        )
        patch.start()
        self.addCleanup(patch.stop)

    def run_gate(self, failing_index=None, report=CLEAN, exit_code=0, launch_error=False):
        calls = []

        def run(command, *, stdout, stderr, check):
            calls.append(command)
            selected = len(calls) - 1 == failing_index
            if selected and launch_error:
                raise OSError('cannot launch checker')
            payload = report if selected else CLEAN
            stdout.write(payload if isinstance(payload, str) else json.dumps(payload))
            stderr.write('diagnostic stderr' if selected else '')
            return subprocess.CompletedProcess(command, exit_code if selected else 0)

        with (
            mock.patch.object(self.runner.subprocess, 'run', side_effect=run),
            redirect_stdout(io.StringIO()),
        ):
            code = self.runner.main()
        self.assertEqual(len(calls), 4)
        for command, (platform, version) in zip(calls, TARGETS, strict=True):
            self.assertEqual(
                command,
                [
                    self.runner.sys.executable,
                    '-m',
                    'pyright',
                    '--pythonplatform',
                    platform,
                    '--pythonversion',
                    version,
                    '--outputjson',
                ],
            )
            self.assertTrue((self.root / f'pyright-{platform}-{version}.json').exists())
            self.assertTrue((self.root / f'pyright-{platform}-{version}.stderr.txt').exists())
        return code

    def test_all_targets_pass_with_distinct_reports_and_named_summaries(self):
        self.assertEqual(self.run_gate(), 0)
        self.assertIn('outcome=pass', (self.root / 'outputs').read_text())
        summary = (self.root / 'summary').read_text()
        for platform, version in TARGETS:
            self.assertIn(f'{platform} / Python {version}', summary)

    def test_any_failure_position_survives_later_successes(self):
        for position in range(4):
            with self.subTest(position=position):
                self.assertEqual(self.run_gate(position, exit_code=2), 1)
        self.assertNotIn('outcome=pass', (self.root / 'outputs').read_text())

    def test_reported_error_fails_even_with_successful_exit(self):
        report = {
            'summary': {'errorCount': 1},
            'generalDiagnostics': [{'severity': 'error', 'message': 'Windows-only API'}],
        }
        self.assertEqual(self.run_gate(0, report=report), 1)
        self.assertIn('Windows-only API', (self.root / 'summary').read_text())

    def test_malformed_and_inconsistent_reports_do_not_stop_remaining_targets(self):
        for report in (
            '',
            '{',
            'null',
            '[]',
            '{}',
            {'summary': {'errorCount': 0}},
            {'summary': {'errorCount': True}, 'generalDiagnostics': []},
            {'summary': {'errorCount': -1}, 'generalDiagnostics': []},
            {'summary': {'errorCount': 1}, 'generalDiagnostics': []},
            {'summary': {'errorCount': 0}, 'generalDiagnostics': [None]},
        ):
            with self.subTest(report=report):
                self.assertEqual(self.run_gate(0, report=report), 1)

    def test_missing_report_does_not_stop_remaining_targets(self):
        read = self.runner.read_report

        def read_report(path):
            if path.name == 'pyright-Linux-3.10.json':
                raise FileNotFoundError('missing report')
            return read(path)

        with mock.patch.object(self.runner, 'read_report', side_effect=read_report):
            self.assertEqual(self.run_gate(), 1)

    def test_launch_failure_cannot_reuse_a_stale_success_report(self):
        stale = self.root / 'pyright-Linux-3.10.json'
        stale.write_text(json.dumps(CLEAN), encoding='utf-8')
        self.assertEqual(self.run_gate(0, launch_error=True), 1)
        self.assertEqual(stale.read_text(), '')
