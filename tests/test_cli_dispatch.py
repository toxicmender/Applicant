"""Check the CLI's typed registration before settings-dependent work begins."""

import argparse
import unittest

from applicant.cli import build_parser
from applicant.errors import ConfigError
from applicant.settings import Settings

FAMILIES = {
    'jobs',
    'search',
    'apply',
    'benchmark',
    'company-evidence',
    'reviews',
    'financials',
    'rates',
    'status',
    'survey',
    'rank',
    'feedback',
    'evaluate',
    'personalize',
    'db',
    'sources',
    'setup',
    'store',
    'research',
    'feed',
    'analytics',
    'report',
}
GROUPS = {
    'research': {
        'template',
        'import',
        'report',
        'capture-profile',
        'bridge-company',
        'backfill-companies',
    },
    'feed': {'collect', 'refresh', 'show'},
    'analytics': {'technologies', 'show'},
    'report': {'render'},
    'sources': {'list', 'show', 'health', 'probe'},
    'setup': {'provider', 'status', 'import-session'},
    'store': {'checkpoint'},
    'benchmark': {'discover', 'fetch', 'import', 'show', 'compare'},
    'company-evidence': {'associate', 'import', 'capture', 'refresh', 'show', 'enrich'},
    'feedback': {'record', 'import', 'list'},
    'personalize': {'suggest', 'show', 'accept'},
    'db': {'upgrade'},
    'survey': {'edit', 'validate', 'import', 'show', 'export'},
    'rank': {'create', 'list', 'show', 'explain', 'compare', 'replay'},
}


def children(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    return {}


class DispatchTest(unittest.TestCase):
    def test_rejected_credentials_are_masked_even_before_settings_load(self):
        import io
        from contextlib import redirect_stderr

        from applicant.cli import main

        for options in (
            ['--crunchbase-key', 'private-key-12345678'],
            ['--tracxn-key=private-key-12345678'],
            ['--tracxn-k', 'private-key-12345678'],
        ):
            err = io.StringIO()
            with self.subTest(options=options), redirect_stderr(err), self.assertRaises(SystemExit):
                main(['company-evidence', 'show', '--snapshot', 'x', *options])
            self.assertNotIn('private-key-12345678', err.getvalue())
            self.assertIn('--', err.getvalue())

    def test_feedback_request_accepts_an_already_validated_event(self):
        from applicant.cli.feedback import Record
        from applicant.domain.feedback import FeedbackInput

        event = FeedbackInput(
            event_id='event', run_id='run', job_id='job', expected_revision=0, decision='shortlist'
        )
        request = Record(event=event)
        self.assertEqual(request.event, event)

    def test_every_executable_leaf_has_a_typed_binding(self):
        families = children(build_parser())
        self.assertEqual(set(families), FAMILIES)
        for name, parser in families.items():
            leaves = children(parser)
            with self.subTest(family=name):
                self.assertEqual(set(leaves), GROUPS.get(name, set()))
            for leaf in leaves.values() if leaves else (parser,):
                with self.subTest(leaf=leaf.prog):
                    self.assertTrue(callable(leaf.get_default('prepare')))
                    self.assertTrue(callable(leaf.get_default('handler')))

    def test_shared_flags_work_at_every_nesting_level(self):
        for argv in (
            ['--data-dir', 'root', 'benchmark', 'fetch', '--query', 'input.json'],
            ['benchmark', '--data-dir', 'root', 'fetch', '--query', 'input.json'],
            ['benchmark', 'fetch', '--query', 'input.json', '--data-dir', 'root'],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(build_parser().parse_args(argv).data_dir, 'root')
        args = build_parser().parse_args(['-vv', 'benchmark', '--quiet', 'fetch', '--query', 'x'])
        self.assertEqual(args.verbose, 2)
        self.assertTrue(args.quiet)

    def test_group_handlers_are_bound_at_parser_construction(self):
        from unittest.mock import patch

        from applicant.cli import main

        with patch('applicant.cli.run_benchmark', return_value=0) as handler:
            self.assertEqual(main(['--no-log-file', 'benchmark', 'fetch', '--query', 'unused']), 0)
        request, settings = handler.call_args.args
        self.assertEqual(request.query, 'unused')
        self.assertIsInstance(settings, Settings)

    def test_refresh_only_options_require_refresh(self):
        from pydantic import ValidationError

        from applicant.cli.rates import Rates

        for field, value in (('force', True), ('currency', ['GBP', 'SEK']), ('into', 'x.json')):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                Rates.model_validate({field: value})
            self.assertTrue(Rates.model_validate({'refresh': True, field: value}).refresh)

    def test_unknown_request_field_is_rejected_without_echoing_input(self):
        args = build_parser().parse_args(['status'])
        args.unexpected = 'sensitive-input'
        with self.assertRaises(ConfigError) as caught:
            args.prepare(args, Settings())
        self.assertIn('--unexpected', str(caught.exception))
        self.assertNotIn('sensitive-input', str(caught.exception))

    def test_negative_filters_fail_before_creating_a_log(self):
        import io
        import tempfile
        from contextlib import redirect_stderr
        from pathlib import Path

        from applicant.cli import main

        with tempfile.TemporaryDirectory() as root:
            for command in (['search', 'python'], ['apply']):
                for flag in ('--experience', '--min-salary'):
                    with self.subTest(command=command, flag=flag), redirect_stderr(io.StringIO()):
                        self.assertEqual(main(['--data-dir', root, *command, flag, '-1']), 2)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_status_keeps_the_json_filename(self):
        args: argparse.Namespace = build_parser().parse_args(['status', '--json', 'out.json'])
        self.assertEqual(args.json, 'out.json')
