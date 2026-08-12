"""The running commentary: where it goes, how much of it, and when it is silent.

Logging is process wide state, so every test here puts it back afterwards -
otherwise a configured handler outlives its test and writes into a buffer
nobody is reading any more.
"""

from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path

from applicant import log


class LogTest(unittest.TestCase):
    def setUp(self):
        log.silence()
        self.addCleanup(log.silence)
        self.stream = io.StringIO()

    def lines(self) -> list[str]:
        return [line for line in self.stream.getvalue().splitlines() if line.strip()]


class NamingTest(LogTest):
    def test_a_module_gets_its_own_logger_under_the_package(self):
        self.assertEqual(log.get('applicant.search').name, 'applicant.search')

    def test_a_bare_name_is_placed_under_the_package(self):
        self.assertEqual(log.get('boards.naukri').name, 'applicant.boards.naukri')

    def test_the_package_name_itself_is_left_alone(self):
        self.assertEqual(log.get('applicant').name, 'applicant')

    def test_a_name_that_merely_starts_the_same_is_not_mistaken_for_ours(self):
        self.assertEqual(log.get('applicants_helper').name, 'applicant.applicants_helper')


class LevelTest(LogTest):
    def test_the_three_settings(self):
        self.assertEqual(log.level_for(log.QUIET), logging.WARNING)
        self.assertEqual(log.level_for(log.NORMAL), logging.INFO)
        self.assertEqual(log.level_for(1), logging.DEBUG)

    def test_quieter_than_quiet_is_still_quiet(self):
        self.assertEqual(log.level_for(-5), logging.WARNING)


class ConsoleTest(LogTest):
    def test_a_normal_run_reads_as_it_always_did(self):
        """These lines were `print` calls; -v is where the decoration starts."""
        log.configure(stream=self.stream)
        log.get('applicant.search').info('naukri: 8 of 11 jobs match')
        self.assertEqual(self.lines(), ['naukri: 8 of 11 jobs match'])

    def test_quiet_keeps_the_failures_and_drops_the_commentary(self):
        log.configure(log.QUIET, stream=self.stream)
        logger = log.get('applicant.search')
        logger.info('naukri: 8 of 11 jobs match')
        logger.warning('naukri: served a bot check')
        self.assertEqual(self.lines(), ['naukri: served a bot check'])

    def test_verbose_says_where_each_line_came_from(self):
        log.configure(1, stream=self.stream)
        log.get('applicant.boards.naukri').debug('intercepted the search api call')

        line = self.lines()[0]
        self.assertIn('applicant.boards.naukri', line)
        self.assertIn('DEBUG', line)
        self.assertIn('intercepted the search api call', line)

    def test_debug_is_not_shown_at_the_usual_level(self):
        log.configure(stream=self.stream)
        log.get('applicant.search').debug('every url in full')
        self.assertEqual(self.lines(), [])

    def test_configuring_twice_does_not_double_every_line(self):
        """`main()` is called dozens of times in one test run."""
        log.configure(stream=self.stream)
        log.configure(stream=self.stream)
        log.get('applicant.search').info('once')
        self.assertEqual(self.lines(), ['once'])

    def test_reconfiguring_moves_the_output(self):
        log.configure(stream=self.stream)
        second = io.StringIO()
        log.configure(stream=second)

        log.get('applicant.search').info('to the new one')
        self.assertEqual(self.stream.getvalue(), '')
        self.assertIn('to the new one', second.getvalue())


class QuietByDefaultTest(LogTest):
    """A library says nothing until its caller asks it to."""

    def test_nothing_is_configured_until_configure_is_called(self):
        handlers = logging.getLogger(log.ROOT).handlers
        self.assertTrue(all(isinstance(one, logging.NullHandler) for one in handlers))

    def test_silence_undoes_configure(self):
        log.configure(stream=self.stream)
        log.silence()
        log.get('applicant.search').warning('into the void')
        self.assertEqual(self.stream.getvalue(), '')

    def test_the_null_handler_survives_silence(self):
        """Without it, logging complains on stderr about having nowhere to go."""
        log.configure(stream=self.stream)
        log.silence()
        handlers = logging.getLogger(log.ROOT).handlers
        self.assertTrue(any(isinstance(one, logging.NullHandler) for one in handlers))

    def test_an_unconfigured_library_still_reaches_its_callers_logging(self):
        """Someone who configured the root logger should hear from us."""
        self.assertTrue(logging.getLogger(log.ROOT).propagate)

    def test_configuring_stops_the_records_being_handled_twice(self):
        """Once we print them, a root handler printing them again is noise."""
        log.configure(stream=self.stream)
        self.assertFalse(logging.getLogger(log.ROOT).propagate)

    def test_silence_hands_the_records_back(self):
        log.configure(stream=self.stream)
        log.silence()
        self.assertTrue(logging.getLogger(log.ROOT).propagate)


class LogFileTest(LogTest):
    def setUp(self):
        super().setUp()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = str(Path(self._dir.name) / 'run.log')

    def written(self) -> str:
        with open(self.path, encoding='utf-8') as handle:
            return handle.read()

    def test_it_records_what_the_console_showed(self):
        log.configure(stream=self.stream, filepath=self.path)
        log.get('applicant.search').info('naukri: 8 of 11 jobs match')
        self.assertIn('naukri: 8 of 11 jobs match', self.written())

    def test_it_keeps_the_detail_the_console_left_out(self):
        """The point of a file is to hold what you did not know you would want."""
        log.configure(log.QUIET, stream=self.stream, filepath=self.path)
        log.get('applicant.search').debug('every url in full')

        self.assertEqual(self.lines(), [], 'the console was asked to be quiet')
        self.assertIn('every url in full', self.written())

    def test_it_is_timestamped_and_attributed(self):
        log.configure(stream=self.stream, filepath=self.path)
        log.get('applicant.boards.indeed').info('read 25 cards')
        self.assertIn('applicant.boards.indeed', self.written())
        self.assertIn('INFO', self.written())

    def test_a_path_that_cannot_be_opened_says_so(self):
        with self.assertRaises(OSError):
            log.configure(filepath=str(Path(self._dir.name) / 'no' / 'such' / 'dir' / 'run.log'))


if __name__ == '__main__':
    unittest.main()
