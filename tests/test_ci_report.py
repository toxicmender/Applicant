""".github/scripts/ci_report.py: the script the `status` job - the one required
check - builds its record and summary with. A crash here fails `status`; a
miscount misreports every run.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent.parent / '.github' / 'scripts' / 'ci_report.py'


def load():
    spec = importlib.util.spec_from_file_location('ci_report', SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def suite(tests=3, failures=0, errors=0, skipped=0, time=1.5, name='pytest') -> str:
    return (
        f'<testsuite name="{name}" tests="{tests}" failures="{failures}" errors="{errors}" '
        f'skipped="{skipped}" time="{time}"></testsuite>'
    )


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.report = load()
        # nothing here may append to a real step summary
        patch = mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''})
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, name: str, text: str) -> Path:
        path = self.root / name
        path.write_text(text, encoding='utf-8')
        return path


class ReadJunitTest(Folder):
    def test_pytest_s_wrapped_suite_and_a_bare_one(self):
        self.write(
            'junit-3.12.xml',
            f'<testsuites name="pytest tests">{suite(tests=10, skipped=2)}</testsuites>',
        )
        self.write('junit-browser.xml', suite(tests=9, failures=1, errors=1))
        suites = self.report.read_junit(str(self.root / 'junit-*.xml'))
        self.assertEqual(
            [(s['file'], s['tests'], s['failures'] + s['errors'], s['skipped']) for s in suites],
            [('junit-3.12.xml', 10, 0, 2), ('junit-browser.xml', 9, 2, 0)],
        )

    def test_every_suite_in_a_report_counts(self):
        """unittest's XML writers put one <testsuite> per class under <testsuites>."""
        self.write(
            'junit-u.xml', f'<testsuites>{suite(tests=4)}{suite(tests=6, failures=1)}</testsuites>'
        )
        (found,) = self.report.read_junit(str(self.root / 'junit-*.xml'))
        self.assertEqual((found['tests'], found['failures']), (10, 1))
        self.assertAlmostEqual(found['time'], 3.0)

    def test_no_reports_at_all(self):
        self.assertEqual(self.report.read_junit(str(self.root / 'junit-*.xml')), [])

    def test_an_unreadable_report_is_said_not_silently_dropped(self):
        self.write('junit-bad.xml', '<testsuites><testsuite tests="3"')
        self.write('junit-empty.xml', '<testsuites></testsuites>')
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(self.report.read_junit(str(self.root / 'junit-*.xml')), [])
        self.assertIn('junit-bad.xml', err.getvalue())

    def test_the_table(self):
        self.assertEqual(self.report.junit_table([]), ['No test reports were produced.'])
        row = self.report.junit_table(
            [{'file': 'j.xml', 'tests': 5, 'failures': 1, 'errors': 1, 'skipped': 1, 'time': 2.25}]
        )[2]
        self.assertEqual(row, '| `j.xml` | 5 | 2 | 1 | 2.2s |')


class PyrightTest(Folder):
    def diagnostic(self, line=4, message='Cannot access attribute', severity='error') -> dict:
        return {
            'file': '/repo/src/applicant/x.py',
            'severity': severity,
            'message': message,
            'range': {'start': {'line': line}},
        }

    def summary(self, diagnostics) -> tuple[int, list[str]]:
        path = self.write('pyright.json', json.dumps({'generalDiagnostics': diagnostics}))
        return self.report.pyright_summary(str(path))

    def test_clean(self):
        self.assertEqual(
            self.summary([self.diagnostic(severity='warning')]),
            (0, ['### ✅ pyright: no type errors']),
        )

    def test_errors_are_listed_one_line_each(self):
        count, lines = self.summary([self.diagnostic(message='first line\n  detail')])
        self.assertEqual(count, 1)
        self.assertIn('x.py:5 - first line', lines)

    def test_an_error_with_no_message_does_not_crash_the_report(self):
        count, lines = self.summary([self.diagnostic(message='')])
        self.assertEqual(count, 1)
        self.assertIn('x.py:5 - ', lines)

    def test_a_long_list_is_cut(self):
        count, lines = self.summary([self.diagnostic(line=n) for n in range(60)])
        self.assertEqual(count, 60)
        self.assertIn('... and 10 more', lines)

    def test_an_unreadable_report(self):
        count, lines = self.report.pyright_summary(str(self.root / 'missing.json'))
        self.assertEqual(count, -1)
        self.assertIn('Could not read', lines[0])


class StatusTest(Folder):
    def test_totals_add_up_across_suites(self):
        suites = [
            {'file': 'a', 'tests': 10, 'failures': 1, 'errors': 2, 'skipped': 3, 'time': 1.0},
            {'file': 'b', 'tests': 5, 'failures': 0, 'errors': 0, 'skipped': 1, 'time': 1.0},
        ]
        with mock.patch.dict(os.environ, {'GITHUB_RUN_NUMBER': '42', 'GITHUB_SHA': 'abc'}):
            record = self.report.build_status({}, suites)
        self.assertEqual(record['totals'], {'tests': 15, 'failed': 3, 'skipped': 4})
        self.assertEqual((record['run_number'], record['sha']), (42, 'abc'))

    def test_a_skipped_job_reads_as_skipped_never_as_passed(self):
        table = self.report.status_table(
            {
                'unit': {'result': 'success', 'outcome': 'pass', 'detail': ''},
                'browser': {'result': 'skipped', 'outcome': '', 'detail': ''},
                'odd': {},
            }
        )
        self.assertIn('| `unit` | ✅ success | pass |  |', table)
        self.assertIn('| `browser` | ⏭️ skipped | n/a |  |', table)
        self.assertIn('| `odd` | ❔ unknown | n/a |  |', table)


class MainTest(Folder):
    def run_main(self, *argv: str) -> tuple[int, str, str]:
        summary = self.root / 'summary.md'
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}),
            redirect_stdout(out),
        ):
            code = self.report.main(list(argv))
        return code, out.getvalue(), summary.read_text(encoding='utf-8') if summary.exists() else ''

    def test_status_writes_the_record_and_the_summary(self):
        self.write('junit-3.12.xml', f'<testsuites>{suite(tests=7, skipped=1)}</testsuites>')
        jobs = self.write(
            'jobs.json', json.dumps({'unit': {'result': 'success', 'outcome': 'pass'}})
        )
        out = self.root / 'status.json'
        code, printed, summary = self.run_main(
            'status',
            '--jobs',
            str(jobs),
            '--junit',
            str(self.root / 'junit-*.xml'),
            '--out',
            str(out),
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out.read_text(encoding='utf-8'))['totals'],
            {'tests': 7, 'failed': 0, 'skipped': 1},
        )
        self.assertIn('7 tests, 0 failed, 1 skipped.', summary)
        self.assertEqual(printed.strip(), summary.strip(), 'the summary is what was printed')

    def test_junit_and_pyright_subcommands(self):
        self.write('junit-a.xml', suite(tests=2))
        code, _, summary = self.run_main('junit', str(self.root / 'junit-*.xml'))
        self.assertEqual(code, 0)
        self.assertIn('| `junit-a.xml` | 2 | 0 | 0 |', summary)

        report = self.write('pyright.json', json.dumps({'generalDiagnostics': []}))
        self.assertEqual(
            self.run_main('pyright', str(report))[0], 0, 'a type error is reported, not raised'
        )
        self.assertEqual(
            self.run_main('pyright', str(self.root / 'none.json'))[0], 1, 'no report at all is'
        )

    def test_the_summary_is_appended_to_not_replaced(self):
        """Every step of a job writes to the same summary file."""
        summary = self.write('summary.md', '### pyright from an earlier step\n')
        with (
            mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(summary)}),
            redirect_stdout(io.StringIO()),
        ):
            self.report.emit(['### this step'])
        self.assertEqual(
            summary.read_text(encoding='utf-8'), '### pyright from an earlier step\n### this step\n'
        )

    def test_without_a_step_summary_it_only_prints(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': ''}), redirect_stdout(out):
            self.report.emit(['line'])
        self.assertEqual(out.getvalue(), 'line\n')


if __name__ == '__main__':
    unittest.main()
