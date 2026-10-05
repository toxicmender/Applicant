"""Saved searches: a standing search in applicant.toml, run by name.

The example is the shortlist `test_target_job_filters` finds with twelve
flags: eight AI/ML postings in Indian metros asking for one to five years.
Saved once, it is `applicant search --saved ai-ml`.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from applicant.cli import main
from applicant.errors import ConfigError
from applicant.search import Jobs
from applicant.settings import Settings
from applicant.storage import load_jobs
from tests.test_target_job_filters import StubBoard, pool, shortlist, titles

AI_ML = """
[searches.ai-ml]
keywords = "ai ml engineer"
source = "naukri"
location = "India"
experience = 3
title = ["ai", "ml", "machine learning", "data scientist"]
"""


class SavedSearchTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.config = self.root / 'applicant.toml'
        self.config.write_text(AI_ML, encoding='utf-8')
        self.output = str(self.root / 'job_listing.json')

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(Jobs, '_client', return_value=StubBoard(pool())),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(
                ['search', *argv, '--config', str(self.config), '--no-log-file', '--store', 'files']
            )
        return code, out.getvalue(), err.getvalue()

    def test_a_saved_search_runs_by_name(self):
        code, _, err = self.run_cli('--saved', 'ai-ml', '-o', self.output)
        self.assertEqual(code, 0, err)
        self.assertEqual(set(titles(load_jobs(self.output))), set(titles(shortlist())))

    def test_a_flag_on_the_command_line_wins(self):
        code, _, _ = self.run_cli('--saved', 'ai-ml', '-t', 'data scientist', '-o', self.output)
        self.assertEqual(code, 0)
        self.assertEqual(titles(load_jobs(self.output)), ['Data Scientist I'])

    def test_keywords_given_win_too(self):
        code, _, err = self.run_cli('something else', '--saved', 'ai-ml', '-o', self.output, '-v')
        self.assertEqual(code, 0)
        self.assertIn("search: 'something else' on naukri", err)

    def test_the_output_can_be_saved_as_well(self):
        self.config.write_text(AI_ML + f'output = "{self.output}"\n', encoding='utf-8')
        code, _, _ = self.run_cli('--saved', 'ai-ml')
        self.assertEqual(code, 0)
        self.assertTrue(Path(self.output).exists())

    def test_an_unknown_name_says_which_there_are(self):
        code, _, err = self.run_cli('--saved', 'ai', '-o', self.output)
        self.assertEqual(code, 2)
        self.assertIn("no saved search 'ai'; saved: ai-ml", err)

    def test_no_keywords_and_no_saved_search_is_a_usage_error(self):
        code, _, err = self.run_cli('-o', self.output)
        self.assertEqual(code, 2)
        self.assertIn('needs keywords', err)

    def test_they_can_be_listed(self):
        code, out, _ = self.run_cli('--list-saved')
        self.assertEqual(code, 0)
        self.assertIn("ai-ml: keywords='ai ml engineer'", out)

    def test_a_typo_in_a_saved_search_is_caught_at_load(self):
        self.config.write_text(AI_ML.replace('experience', 'experiance'), encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'searches.ai-ml.experiance'):
            Settings.load(config=str(self.config), environ={})

    def test_a_count_of_zero_is_caught_at_load(self):
        self.config.write_text(AI_ML + 'want = 0\n', encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'searches.ai-ml.want'):
            Settings.load(config=str(self.config), environ={})

    def test_a_count_of_zero_is_a_usage_error_on_the_command_line(self):
        for flag, value in (('--want', '0'), ('--limit', '-5'), ('--posted-within', '0')):
            with self.subTest(flag=flag):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as exit:
                    main(['search', 'x', flag, value, '--no-log-file'])
                self.assertEqual(exit.exception.code, 2)
                self.assertIn(flag, err.getvalue())
                self.assertIn('must be 1 or more', err.getvalue())

    def test_an_unknown_board_is_caught_at_load(self):
        self.config.write_text(AI_ML.replace('"naukri"', '"monster"'), encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'source'):
            Settings.load(config=str(self.config), environ={})


if __name__ == '__main__':
    unittest.main()
