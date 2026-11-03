"""The exported smoke command exercises real CLI processes on golden inputs."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RankingJourneyTest(unittest.TestCase):
    def test_subprocess_journey_ignores_host_settings_and_matches_golden_summary(self):
        environment = dict(os.environ)
        environment.update(
            APPLICANT_CONFIG='must-not-read.toml',
            APPLICANT_STORE='files',
            APPLICANT_HOME='must-not-write',
            PYTHONIOENCODING='utf-8',
        )
        with tempfile.TemporaryDirectory() as root:
            completed = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'smoke_ranking.py')],
                cwd=root,
                env=environment,
                capture_output=True,
                encoding='utf-8',
                timeout=90,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(list(Path(root).iterdir()), [])
        actual = json.loads(completed.stdout)
        expected = json.loads(
            (ROOT / 'tests' / 'fixtures' / 'ranking' / 'cli-journey.json').read_text(
                encoding='utf-8'
            )
        )
        self.assertEqual(actual, {key: expected[key] for key in actual})
