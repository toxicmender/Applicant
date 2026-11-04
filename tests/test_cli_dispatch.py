"""Check the CLI's typed registration before settings-dependent work begins."""

import argparse
import unittest

from applicant.cli import build_parser
from applicant.errors import ConfigError
from applicant.settings import Settings


class DispatchTest(unittest.TestCase):
    def test_unknown_request_field_is_rejected_without_echoing_input(self):
        args = build_parser().parse_args(['status'])
        args.unexpected = 'sensitive-input'
        with self.assertRaises(ConfigError) as caught:
            args.prepare(args, Settings())
        self.assertIn('--unexpected', str(caught.exception))
        self.assertNotIn('sensitive-input', str(caught.exception))

    def test_status_keeps_the_json_filename(self):
        args: argparse.Namespace = build_parser().parse_args(['status', '--json', 'out.json'])
        self.assertEqual(args.json, 'out.json')
