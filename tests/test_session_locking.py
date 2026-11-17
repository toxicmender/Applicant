import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from applicant.errors import ProfileBusy
from applicant.infra.owned_worker import CleanupUnconfirmed
from applicant.infra.path_identity import comparison_path, physical_path
from applicant.infra.profile_lock import profile_lock


def retained_lock(path, connection):
    try:
        with profile_lock(path):
            raise CleanupUnconfirmed('synthetic cleanup failure')
    except CleanupUnconfirmed:
        connection.send('retained')
        connection.recv()


class SessionLockingTest(unittest.TestCase):
    def test_windows_prefixes_and_device_names(self):
        self.assertEqual(comparison_path(r'\\?\C:\folder', windows=True), r'C:\folder')
        self.assertEqual(comparison_path(r'\\?\UNC\server\share', windows=True), r'\\server\share')
        for path in (r'\\.\pipe\name', r'\\?\GLOBALROOT\name'):
            with self.assertRaises(ValueError):
                comparison_path(path, windows=True)

    def test_lock_rejects_linked_leaf_without_changing_contents(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'target'
            target.write_bytes(b'unrelated data')
            path = target.with_name('session.lock')
            os.link(target, path)
            with self.assertRaisesRegex(OSError, 'one link'), profile_lock(path):
                self.fail('linked leaf was locked')
            self.assertEqual(target.read_bytes(), b'unrelated data')

    @unittest.skipIf(os.name == 'nt', 'symlink creation may require Windows privileges')
    def test_missing_suffix_does_not_hide_dangling_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'link'
            path.symlink_to(path.with_name('missing'), target_is_directory=True)
            with self.assertRaises(FileNotFoundError):
                physical_path(path / 'future')

    @unittest.skipIf(os.name == 'nt', 'symlink creation may require Windows privileges')
    def test_resolver_enforces_a_32_link_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ordinary = root / 'target'
            ordinary.touch()
            leaf = ordinary
            for index in range(32):
                alias = root / str(index)
                alias.symlink_to(leaf.name)
                leaf = alias
            self.assertEqual(physical_path(leaf), ordinary.resolve())
            extra = root / 'extra'
            extra.symlink_to(leaf.name)
            with self.assertRaisesRegex(OSError, 'too many filesystem links'):
                physical_path(extra)

    @unittest.skipIf(os.name == 'nt', 'Windows denies unlink while a lock handle is open')
    def test_posix_retries_a_leaf_removed_during_acquisition(self):
        from applicant.infra.advisory_lock import _take_lock

        with tempfile.TemporaryDirectory() as folder:
            leaf = Path(folder) / 'profile.lock'
            calls = []

            def remove_once(descriptor):
                _take_lock(descriptor)
                if not calls:
                    leaf.unlink()
                calls.append(descriptor)

            with (
                patch('applicant.infra.advisory_lock._take_lock', side_effect=remove_once),
                profile_lock(leaf),
            ):
                self.assertTrue(leaf.is_file())
            self.assertEqual(len(calls), 2)

    def test_nested_alias_and_recreated_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'new' / 'session.lock'
            with profile_lock(path), self.assertRaises(ProfileBusy), profile_lock(path.absolute()):
                pass
            path.unlink()
            path.parent.rmdir()
            with profile_lock(path):
                self.assertTrue(path.is_file())

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
