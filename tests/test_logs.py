"""applicant.logs: what reaches a log line, and what never does."""

from __future__ import annotations

import io
import logging
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from applicant import logs
from applicant.cli import main


def record(message: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord('applicant.test', level, __file__, 1, message, None, None)


class FormatterTest(unittest.TestCase):
    def setUp(self):
        self.formatter = logs.SafeFormatter()
        self.addCleanup(logs._secrets.clear)

    def test_a_newline_cannot_forge_a_second_entry(self):
        """ASVS 16.4.1 / CWE-117: a scraped company name is attacker controlled."""
        line = self.formatter.format(record('Acme\n2026-01-01T00:00:00Z ERROR forged'))
        self.assertNotIn('\n', line)
        self.assertIn('Acme\\x0a2026', line)

    def test_other_control_characters_are_escaped_too(self):
        line = self.formatter.format(record('bell\x07 escape\x1b[31m cr\r'))
        self.assertEqual(line.count('\\x'), 3)
        self.assertNotIn('\x1b', line)

    def test_timestamps_are_utc_and_say_so(self):
        """ASVS 16.2.2."""
        self.assertRegex(self.formatter.format(record('x')), r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ')

    def test_each_entry_names_its_level_and_module(self):
        """ASVS 16.2.1."""
        self.assertIn(' WARNING applicant.test: hi', self.formatter.format(record('hi', 30)))

    def test_a_registered_secret_is_masked(self):
        """ASVS 16.2.5."""
        logs.register_secret('cb-key-0123456789')
        line = self.formatter.format(record('GET ?user_key=cb-key-0123456789 failed'))
        self.assertNotIn('cb-key-0123456789', line)
        self.assertIn('user_key=***', line)

    def test_a_secret_inside_a_traceback_is_masked(self):
        logs.register_secret('tx-token-abcdefgh')
        try:
            raise ValueError('token tx-token-abcdefgh rejected')
        except ValueError:
            entry = record('failed')
            entry.exc_info = sys.exc_info()
        self.assertNotIn('tx-token-abcdefgh', self.formatter.format(entry))

    def test_short_values_are_not_treated_as_secrets(self):
        logs.register_secret('abc')
        self.assertIn('abc', self.formatter.format(record('abc')))


class SetupTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(logs.setup)  # leave a plain console handler behind
        self.path = str(Path(self._dir.name) / 'run.log')

    def test_the_log_file_is_readable_by_its_owner_only(self):
        """ASVS 16.4.2."""
        logs.setup(log_file=self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_the_file_gets_debug_detail_the_console_does_not(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            logs.setup(log_file=self.path)
            logging.getLogger('applicant.test').debug('detail')
        for handler in logging.getLogger('applicant').handlers:
            handler.flush()
        self.assertIn('detail', Path(self.path).read_text(encoding='utf-8'))
        self.assertNotIn('detail', stderr.getvalue())

    def test_repeated_setup_does_not_stack_handlers(self):
        for _ in range(3):
            logs.setup()
        self.assertEqual(len(logging.getLogger('applicant').handlers), 1)

    def test_verbosity_levels(self):
        for verbosity, level in ((-1, logging.ERROR), (0, logging.WARNING), (2, logging.DEBUG)):
            with self.subTest(verbosity=verbosity):
                logs.setup(verbosity)
                self.assertEqual(logging.getLogger('applicant').handlers[0].level, level)


class CliLoggingTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(logs.setup)
        self.addCleanup(logs._secrets.clear)
        self.root = Path(self._dir.name)

    def test_every_subcommand_takes_the_logging_flags(self):
        path = str(self.root / 'status.log')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(['status', '-i', str(self.root / 'none.json'), '-vv', '--log-file', path])
        self.assertEqual(code, 0)
        self.assertIn('applicant.cli: status:', Path(path).read_text(encoding='utf-8'))

    def test_api_keys_on_the_command_line_are_never_logged(self):
        path = str(self.root / 'financials.log')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            main(
                [
                    'financials',
                    '-s',
                    'tracxn',
                    '--tracxn-key',
                    'secret-token-123456',
                    '--log-file',
                    path,
                ]
            )
        text = Path(path).read_text(encoding='utf-8')
        self.assertIn("'tracxn_key': '***'", text)
        self.assertNotIn('secret-token-123456', text)


if __name__ == '__main__':
    unittest.main()
