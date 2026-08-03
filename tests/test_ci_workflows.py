"""The CI workflows, checked against the repository they run in.

A workflow that references a file git never committed passes review, passes
locally, and fails only on a runner - which is exactly what happened: an
unanchored `[Ss]cripts` pattern in .gitignore silently excluded
.github/scripts/, so three jobs died on a missing file.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / '.github' / 'workflows'

# `python .github/scripts/thing.py`, `bash foo/bar.sh`, and so on
SCRIPT_CALL = re.compile(r'(?:python3?|bash|sh)\s+(\.github/[\w./-]+\.(?:py|sh))')


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
            relative = str(workflow.relative_to(ROOT))
            with self.subTest(workflow=relative):
                self.assertIn(relative, tracked)


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
        ):
            with self.subTest(path=path):
                self.assertTrue(self.ignored(path), '{} should be ignored'.format(path))

    def test_the_checked_in_ppp_table_is_not_excluded(self):
        """It sits next to the runtime cache and must not be mistaken for it."""
        self.assertFalse(self.ignored('src/applicant/ppp_factors.json'))


if __name__ == '__main__':
    unittest.main()
