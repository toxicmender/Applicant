"""The CI workflows, checked against the repository they run in.

A workflow that references a file git never committed passes review, passes
locally, and fails only on a runner - which is exactly what happened: an
unanchored `[Ss]cripts` pattern in .gitignore silently excluded
.github/scripts/, so three jobs died on a missing file.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'

# `python .github/scripts/thing.py`, `bash foo/bar.sh`, and so on
SCRIPT_CALL = re.compile(r'(?:python3?|bash|sh)\s+((?:\.github|tools)/[\w./-]+\.(?:py|sh))')


def tracked_files() -> set[str]:
    """Everything git actually has, which is not the same as everything on disk."""
    listing = subprocess.run(
        ['git', 'ls-files'], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return set(listing.stdout.split())


class WorkflowScriptsTest(unittest.TestCase):
    def workflows(self) -> list[Path]:
        return sorted(WORKFLOWS.glob('*.yml')) + sorted(WORKFLOWS.glob('*.yaml'))

    def test_the_workflows_are_there(self):
        self.assertTrue(self.workflows(), 'no workflow files found')

    def test_every_referenced_script_exists_on_disk(self):
        for workflow in self.workflows():
            for script in SCRIPT_CALL.findall(workflow.read_text(encoding='utf-8')):
                with self.subTest(workflow=workflow.name, script=script):
                    self.assertTrue(
                        (ROOT / script).is_file(),
                        '{} runs {}, which does not exist'.format(workflow.name, script),
                    )

    def test_every_referenced_script_is_tracked_by_git(self):
        """On disk is not enough - the runner only gets what was committed."""
        tracked = tracked_files()
        for workflow in self.workflows():
            for script in SCRIPT_CALL.findall(workflow.read_text(encoding='utf-8')):
                with self.subTest(workflow=workflow.name, script=script):
                    self.assertIn(
                        script,
                        tracked,
                        '{} runs {}, which git is not tracking - check .gitignore'.format(
                            workflow.name, script
                        ),
                    )

    def test_the_workflows_themselves_are_tracked(self):
        tracked = tracked_files()
        for workflow in self.workflows():
            relative = workflow.relative_to(ROOT).as_posix()
            with self.subTest(workflow=relative):
                self.assertIn(relative, tracked)


class GateTest(unittest.TestCase):
    """`status` is the one required check, so it must see every job and gate on
    the ones that mean a broken program."""

    def setUp(self):
        self.text = (WORKFLOWS / 'ci.yml').read_text(encoding='utf-8')

    def test_workflows_use_locked_project_tools(self):
        for path in WORKFLOWS.glob('*.yml'):
            text = path.read_text(encoding='utf-8')
            with self.subTest(workflow=path.name):
                self.assertNotIn('uvx ', text)
                self.assertNotIn('--with coverage', text)
                self.assertIn('uv sync --locked', text)
                self.assertNotRegex(text, r'uv sync(?! --locked)')

    def test_status_waits_for_every_other_job(self):
        body = self.text.split('\njobs:\n', 1)[1]
        jobs = set(re.findall(r'^  ([a-z][\w-]*):$', body, re.MULTILINE)) - {'status'}
        found = re.search(r'^    needs: \[(.*)\]$', body, re.MULTILINE)
        assert found is not None
        self.assertEqual({name.strip() for name in found.group(1).split(',')}, jobs)

    def test_the_gate_holds_the_tests_types_coverage_and_browser(self):
        gate = self.text.split('- name: Gate', 1)[1]
        for job in ('types', 'unit', 'coverage', 'browser'):
            with self.subTest(job=job):
                self.assertIn('needs.{}.result'.format(job), gate)

    def test_the_browser_tier_runs_in_the_browser_job_and_only_there(self):
        live = 'tests/test_live_pages.py'
        body = self.text.split('\n  browser:\n', 1)[1].split('\n  status:\n', 1)[0]
        self.assertIn('pytest ' + live, body, 'the browser job runs it')
        self.assertIn('--ignore=' + live, self.text, 'the unit legs leave it to that job')

    def test_the_browser_job_may_not_skip(self):
        self.assertIn("APPLICANT_BROWSER_TESTS: '1'", self.text)

    def test_windows_unit_leg_is_in_the_gated_matrix(self):
        unit = self.text.split('\n  unit:\n', 1)[1].split('\n  coverage:\n', 1)[0]
        self.assertIn('runs-on: ${{ matrix.os }}', unit)
        self.assertIn('os: [ubuntu-latest]', unit)
        self.assertRegex(unit, r"os: windows-latest\s+python: '3.12'")
        self.assertIn('shell: bash', unit)
        self.assertIn('uv run --frozen pyright', unit)
        self.assertIn('name: junit-${{ matrix.os }}-${{ matrix.python }}', unit)
        self.assertIn('--junitxml="junit-${{ matrix.os }}-${{ matrix.python }}.xml"', unit)

    def test_installed_ranking_journey_gates_every_unit_platform(self):
        unit = self.text.split('\n  unit:\n', 1)[1].split('\n  coverage:\n', 1)[0]
        command = 'uv run --frozen --isolated --no-editable --python ${{ matrix.python }} python tools/smoke_ranking.py --require-installed'
        self.assertIn(command, unit)
        step = unit.split('- name: Installed ranking CLI journey', 1)[1].split('- name:', 1)[0]
        self.assertNotIn('continue-on-error', step)
        self.assertNotIn('|| true', step)
        self.assertNotIn('if:', step)

    def test_installed_m4_journey_and_module_floor_are_required(self):
        unit = self.text.split('\n  unit:\n', 1)[1].split('\n  coverage:\n', 1)[0]
        step = unit.split('- name: Installed Milestone 4 CLI journey', 1)[1].split('- name:', 1)[0]
        self.assertIn(
            '--isolated --no-editable --refresh-package applicant --reinstall-package applicant',
            step,
        )
        self.assertIn(
            '--python ${{ matrix.python }} python tools/smoke_milestone4.py --require-installed',
            step,
        )
        for escape in ('continue-on-error', '|| true', 'if:'):
            self.assertNotIn(escape, step)
        coverage = self.text.split('\n  coverage:\n', 1)[1].split('\n  browser:\n', 1)[0]
        self.assertIn('coverage json -q -o coverage-m4.json', coverage)
        self.assertIn(
            'if ! uv run --frozen python tools/check_milestone_coverage.py coverage-m4.json',
            coverage,
        )
        gate = coverage.split('if ! uv run --frozen python tools/check_milestone_coverage.py', 1)[
            1
        ].split('fi', 1)[0]
        self.assertIn('exit 1', gate)
        self.assertIn('milestone-4-coverage.json', coverage)

    def test_the_coverage_floor_is_set_where_a_local_run_reads_it(self):
        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib  # pyright: ignore[reportMissingImports] - 3.10 only

        config = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['tool'][
            'coverage'
        ]
        self.assertGreaterEqual(config['report']['fail_under'], 90)
        self.assertTrue(config['run']['branch'])


class GitignoreTest(unittest.TestCase):
    """The virtualenv block is unanchored in the upstream template it came from."""

    def ignored(self, path: str) -> bool:
        result = subprocess.run(['git', 'check-ignore', '-q', path], cwd=ROOT, capture_output=True)
        return result.returncode == 0

    def test_dot_github_is_never_excluded(self):
        for path in (
            '.github/scripts/ci_report.py',
            '.github/workflows/ci.yml',
            '.github/workflows/format.yml',
        ):
            with self.subTest(path=path):
                self.assertFalse(self.ignored(path), '{} is gitignored'.format(path))

    def test_package_directories_are_not_swallowed_by_venv_patterns(self):
        """`bin`, `lib` and `scripts` are ordinary names inside a package."""
        for path in (
            'src/applicant/bin/thing.py',
            'src/applicant/lib/thing.py',
            'src/applicant/scripts/thing.py',
            'docs/include/thing.md',
        ):
            with self.subTest(path=path):
                self.assertFalse(self.ignored(path), '{} is gitignored'.format(path))

    def test_a_root_virtualenv_is_still_excluded(self):
        for path in ('bin/python', 'lib/site-packages/x.py', '.venv/pyvenv.cfg'):
            with self.subTest(path=path):
                self.assertTrue(self.ignored(path), '{} should be ignored'.format(path))

    def test_the_runtime_artefacts_stay_excluded(self):
        for path in (
            'cookies.json',
            'job_listing.json',
            'applied_jobs.csv',
            'company_reviews.json',
            '.money_cache.json',
            'company_financials.json',
            'job_listing.json.corrupt-20260927T084242Z',
            'company_financials.json.corrupt-20260927T084242Z',
        ):
            with self.subTest(path=path):
                self.assertTrue(self.ignored(path), '{} should be ignored'.format(path))

    def test_the_checked_in_ppp_table_is_not_excluded(self):
        """It sits next to the runtime cache and must not be mistaken for it."""
        self.assertFalse(self.ignored('src/applicant/ppp_factors.json'))


if __name__ == '__main__':
    unittest.main()
