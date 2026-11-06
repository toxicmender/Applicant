"""The smoke table isolates host settings and uses exactly two launchers."""

import io
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tools.smoke_cli import check

from applicant import __version__


class SmokeTest(unittest.TestCase):
    def test_settings_are_isolated_and_launch_count_is_fixed(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f'applicant {__version__}\n', stderr=''
        )
        with (
            patch.dict(
                os.environ, {'APPLICANT_CONFIG': 'absent.toml', 'APPLICANT_STORE': 'invalid'}
            ),
            patch('tools.smoke_cli.subprocess.run', return_value=result) as launch,
            redirect_stdout(io.StringIO()),
        ):
            check(require_installed=False)
            self.assertEqual(os.environ['APPLICANT_STORE'], 'invalid')
        self.assertEqual(launch.call_count, 2)
        executable, module = [call.args[0] for call in launch.call_args_list]
        self.assertEqual(executable[-1], '--version')
        self.assertEqual(module[-3:], ['-m', 'applicant', '--version'])
