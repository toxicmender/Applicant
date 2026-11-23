import tempfile
import unittest
from pathlib import Path

from applicant.errors import ConfigError
from applicant.infra.session_store import SessionStore

STATE = {
    'cookies': [{'name': 'session', 'value': 'synthetic-secret', 'domain': '.indeed.com'}],
    'origins': [],
}


class SessionStoreTest(unittest.TestCase):
    def test_encrypted_profile_survives_move_and_rejects_different_provider(self):
        keys = {}

        def keyring(action, account, value=None):
            if action == 'set':
                keys[account] = value
            return keys.get(account)

        with tempfile.TemporaryDirectory() as folder:
            original = Path(folder) / 'before'
            original.mkdir()
            store = SessionStore(original / 'storage_state.json', 'indeed', keyring=keyring)
            saved = store.install(store.snapshot(), STATE, 'verified')
            assert saved is not None
            self.assertEqual(saved['generation'], 1)
            self.assertNotIn(b'synthetic-secret', store.path.read_bytes())
            moved = original.rename(Path(folder) / 'after')
            reopened = SessionStore(moved / store.path.name, 'indeed', keyring=keyring)
            self.assertEqual(reopened.credentials(reopened.snapshot()), STATE)
            with self.assertRaises(ConfigError):
                SessionStore(reopened.path, 'naukri', keyring=keyring).snapshot()

    def test_stale_install_cannot_supersede_clear(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'storage_state.json', 'indeed', mode='plaintext')
            earlier = store.snapshot()
            store.clear()
            self.assertIsNone(store.install(earlier, STATE, 'verified'))
            self.assertIsNone(store.credentials(store.snapshot()))

    def test_pending_database_survives_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'storage_state.json'
            store = SessionStore(path, 'indeed', mode='plaintext')
            store.install(
                store.snapshot(),
                STATE,
                'verified',
                pending='recover',
                metadata={'pending_database': 'selected.db'},
            )

            def busy(action, database):
                self.assertEqual((action, database), ('recover', 'selected.db'))
                return {'status': 'database_busy', 'retryable': True}

            store.reconcile(busy)
            reopened = SessionStore(path, 'indeed', mode='plaintext')
            self.assertEqual(reopened.snapshot()['pending_database'], 'selected.db')
            reopened.clear(database='other.db')
            observed = []

            def recovered(action, database):
                observed.append((action, database))
                return {'status': 'recovered'}

            reopened.reconcile(recovered)
            self.assertEqual(observed, [('clear', 'other.db')])
            self.assertIsNone(reopened.snapshot()['pending'])
