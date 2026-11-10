import time
import unittest

from applicant.infra.owned_worker import WorkerFailure, run_worker


def echo(connection, arguments):
    connection.send(('closing', None))
    return arguments


def hanging(connection, arguments):
    connection.send(('closing', None))
    time.sleep(30)


def failing(connection, arguments):
    raise ValueError('private data must not leave the worker')


class OwnedWorkerTest(unittest.TestCase):
    def test_private_roundtrip(self):
        self.assertEqual(run_worker(echo, {'synthetic': 'state'}), {'synthetic': 'state'})

    def test_hung_close_is_bounded(self):
        with self.assertRaisesRegex(WorkerFailure, 'deadline'):
            run_worker(hanging, None, grace=0.2)

    def test_failure_does_not_disclose_payload(self):
        with self.assertRaisesRegex(WorkerFailure, 'ValueError') as raised:
            run_worker(failing, None)
        self.assertNotIn('private data', str(raised.exception))
