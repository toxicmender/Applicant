import unittest
from unittest.mock import Mock, patch

from applicant.services.provider_setup import _verification_worker, verify_state


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
