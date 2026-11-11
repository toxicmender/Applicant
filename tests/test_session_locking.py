import multiprocessing
import tempfile
import unittest
from pathlib import Path

from applicant.errors import ProfileBusy
from applicant.infra.owned_worker import CleanupUnconfirmed
from applicant.infra.profile_lock import profile_lock


def retained_lock(path, connection):
    try:
        with profile_lock(path):
            raise CleanupUnconfirmed('synthetic cleanup failure')
    except CleanupUnconfirmed:
        connection.send('retained')
        connection.recv()


class SessionLockingTest(unittest.TestCase):
    def test_process_contention_and_death_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.lock'
            context = multiprocessing.get_context('spawn')
            parent, child_pipe = context.Pipe()
            child = context.Process(target=retained_lock, args=(path, child_pipe))
            child.start()
            child_pipe.close()
            try:
                self.assertTrue(parent.poll(60), 'child did not report retained profile lock')
                self.assertEqual(parent.recv(), 'retained')
                with self.assertRaises(ProfileBusy), profile_lock(Path(folder) / '.' / path.name):
                    pass
            finally:
                parent.close()
                if child.is_alive():
                    child.terminate()
                    child.join(60)
                child.close()
            with profile_lock(path):
                pass
