import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from applicant.services.provider_setup import (
    _verification_worker,
    import_session,
    session_status,
    verify_state,
)
from applicant.settings import Settings
from tests.test_setup_lifecycle import STATE


class VerificationWorkerTest(unittest.TestCase):
    def test_verification_dispatch_and_protocol(self):
        state = {'cookies': [], 'origins': []}
        with patch('applicant.infra.owned_worker.run_worker', return_value=state) as worker:
            self.assertIs(verify_state('linkedin', state, interactive=True), state)
            self.assertEqual(
                worker.call_args.args, (_verification_worker, ('linkedin', state, True))
            )
            self.assertTrue(worker.call_args.kwargs['interactive'])
        connection = Mock()

        def inline(name, state, *, factory, pause, interactive):
            self.assertEqual((name, interactive), ('linkedin', True))
            pause('login prompt')
            session = factory(storage_state=state)
            session.__exit__(None, None, None)
            return state

        with patch('applicant.services.provider_setup._verify_inline', side_effect=inline):
            self.assertIs(_verification_worker(connection, ('linkedin', state, True)), state)
        self.assertEqual(connection.send.call_args_list[0].args[0], ('pause', 'login prompt'))
        connection.recv.assert_called_once()
        connection.send.assert_called_with(('closing', None))

    def test_database_contention_preserves_installed_verified_session(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, database = root / 'incoming.json', root / 'existing.db'
            source.write_text(json.dumps(STATE), encoding='utf-8')
            database.touch()
            settings = Settings(data_dir=root)
            with patch(
                'applicant.services.provider_health.sqlite3.connect',
                side_effect=sqlite3.OperationalError('database is locked'),
            ):
                result = import_session(
                    settings,
                    'linkedin',
                    source,
                    verify=True,
                    verifier=Mock(return_value=STATE),
                    database=str(database),
                )
            self.assertEqual(result['status'], 'verified')
            self.assertEqual(
                result['health_recovery'], {'status': 'database_busy', 'retryable': True}
            )
            self.assertEqual(session_status(settings, 'linkedin')['status'], 'verified')
