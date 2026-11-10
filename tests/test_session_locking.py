import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from applicant.errors import ProfileBusy
from applicant.infra.profile_lock import profile_lock


class SessionLockingTest(unittest.TestCase):
    def test_process_contention_and_death_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'session.lock'
            code = (
                'import sys,time; from pathlib import Path; '
                'from applicant.infra.profile_lock import profile_lock; '
                'lock=profile_lock(Path(sys.argv[1])); lock.__enter__(); '
                'print("locked",flush=True); time.sleep(60)'
            )
            child = subprocess.Popen(
                [sys.executable, '-c', code, str(path)], stdout=subprocess.PIPE, text=True
            )
            try:
                self.assertIsNotNone(child.stdout)
                assert child.stdout is not None
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                with self.assertRaises(ProfileBusy), profile_lock(Path(folder) / '.' / path.name):
                    pass
            finally:
                child.terminate()
                child.wait(timeout=5)
                if child.stdout:
                    child.stdout.close()
            with profile_lock(path):
                pass
