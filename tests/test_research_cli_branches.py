import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from applicant.cli import main
from applicant.cli.setup import legacy_login, output_result
from applicant.infra.store.sqlite import Store
from applicant.services.provider_health import acquire, finish, health, recover_authentication
from applicant.settings import Settings
from tests.research_fixtures import NOW


class ResearchCLIBranchesTest(unittest.TestCase):
    def test_file_only_preflight_and_recovery_failure(self):
        with patch('applicant.cli.setup.setup_provider') as setup, redirect_stdout(io.StringIO()):
            self.assertNotEqual(
                main(
                    [
                        '--no-log-file',
                        '--store',
                        'files',
                        'setup',
                        'provider',
                        'linkedin',
                        '--database',
                        'db',
                    ]
                ),
                0,
            )
            setup.assert_not_called()
        for database in (None, 'chosen.db'):
            with (
                patch(
                    'applicant.cli.setup.recover_authentication',
                    return_value={'status': 'database_busy', 'retryable': True},
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    output_result(
                        {'provider': 'linkedin', 'status': 'verified'}, Settings(), database
                    ),
                    8,
                )
                self.assertIn('--check', json.loads(output.getvalue())['command'])

    def test_setup_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            for command, service in [
                (['provider', 'linkedin'], 'setup_provider'),
                (['status', '--provider', 'linkedin', '--check'], 'session_status'),
                (
                    [
                        'import-session',
                        '--provider',
                        'linkedin',
                        '--from-file',
                        'state.json',
                        '--verify',
                    ],
                    'import_session',
                ),
            ]:
                for status in ('needs_setup', 'verified', 'unverified'):
                    with (
                        self.subTest(command=command, status=status),
                        patch(
                            'applicant.cli.setup.' + service,
                            return_value={'provider': 'linkedin', 'status': status},
                        ),
                        redirect_stdout(io.StringIO()) as output,
                    ):
                        code = main(['--data-dir', folder, '--no-log-file', 'setup', *command])
                        self.assertEqual(code, 3 if status == 'needs_setup' else 0)
                        self.assertEqual(json.loads(output.getvalue())['status'], status)

    def test_legacy_override_paths(self):
        with (
            patch(
                'applicant.cli.setup.setup_provider',
                return_value={'provider': 'linkedin', 'status': 'needs_setup'},
            ) as service,
            redirect_stdout(io.StringIO()),
        ):
            for name in ('glassdoor', 'crunchbase', 'tracxn'):
                self.assertEqual(legacy_login(Settings(), name, profile='custom'), 3)
                self.assertEqual(
                    getattr(service.call_args.args[0].files, name + '_profile'), 'custom'
                )
            legacy_login(Settings(), 'linkedin', cookies='custom.json')
            self.assertEqual(service.call_args.args[0].files.cookies, 'custom.json')
            legacy_login(Settings(), 'linkedin')

    def test_sources_commands(self):
        with tempfile.TemporaryDirectory() as folder:

            def call(*args):
                with redirect_stdout(io.StringIO()) as output:
                    code = main(['--data-dir', folder, '--no-log-file', 'sources', *args])
                return code, json.loads(output.getvalue())

            self.assertTrue(call('list')[1]['providers'])
            self.assertEqual(call('show', 'otta')[1]['providers'][0]['id'], 'welcometothejungle')
            self.assertEqual(call('health', '--provider', 'indeed')[1]['status'], 'ready')
            database = Path(folder) / 'db'
            with Store(database):
                pass
            self.assertEqual(
                call('health', '--provider', 'indeed', '--database', str(database))[0], 0
            )
            for versions in ([], ['job']):
                with patch(
                    'applicant.cli.sources.collect_feed', return_value={'job_version_ids': versions}
                ) as collect:
                    self.assertEqual(
                        call('probe', '--provider', 'indeed', '--database', str(database))[0],
                        int(not versions),
                    )
                    self.assertTrue(collect.call_args.kwargs['probe'])

    def test_recovery_does_not_create_or_clear_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'db'
            self.assertEqual(
                recover_authentication(database, 'linkedin')['status'], 'skipped_missing_database'
            )
            self.assertFalse(database.exists())
            lease = acquire(database, 'linkedin', clock=lambda: NOW)
            finish(database, 'linkedin', lease['token'], 'needs_setup', clock=lambda: NOW)
            self.assertEqual(recover_authentication(database, 'linkedin')['status'], 'recovered')
            lease = acquire(database, 'linkedin', clock=lambda: NOW)
            finish(database, 'linkedin', lease['token'], 'blocked', clock=lambda: NOW)
            self.assertEqual(recover_authentication(database, 'linkedin')['status'], 'unchanged')
            self.assertEqual(health(database, 'linkedin')['status'], 'cooldown')
