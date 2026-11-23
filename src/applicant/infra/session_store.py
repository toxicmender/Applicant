"""Private, revision-checked storage for provider authentication state."""

from __future__ import annotations

import json
import stat
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken

from ..errors import ApplicantError, ConfigError, ProfileBusy
from ..files import document_bytes, write_document_bytes
from ..permissions import make_private_directory
from .advisory_lock import advisory_lock
from .owned_worker import CleanupUnconfirmed, WorkerFailure, run_worker
from .path_identity import physical_path
from .profile_lock import profile_lock

KEY_SERVICE = 'applicant.sessions.v1'
FORMAT = 'applicant-session-v1'
_UNSET = object()


class SessionIntegrityError(ApplicantError):
    """The stored record cannot safely be used or replaced."""


def _keyring_worker(connection, arguments):
    import sys

    action, account, value = arguments
    if sys.platform == 'win32':
        from keyring.backends.Windows import WinVaultKeyring

        backend = WinVaultKeyring()
    elif sys.platform == 'darwin':
        from keyring.backends.macOS import Keyring

        backend = Keyring()
    else:
        from keyring.backends.SecretService import Keyring

        backend = Keyring()
    current = backend.get_password(KEY_SERVICE, account)
    if action == 'set' and current is None:
        backend.set_password(KEY_SERVICE, account, value)
        return value
    if action == 'delete' and current is not None:
        backend.delete_password(KEY_SERVICE, account)
        return None
    return current


def session_keyring(action, account, value=None):
    try:
        return run_worker(_keyring_worker, (action, account, value), timeout=3)
    except CleanupUnconfirmed:
        raise
    except WorkerFailure:
        raise ConfigError(
            'key_store_unavailable: unlock the OS keyring or configure session_storage = "plaintext"'
        ) from None


def _read(path):
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & 0x400
        ):
            raise SessionIntegrityError('session must be a private regular file with one link')
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise SessionIntegrityError('session record is not valid JSON') from None
    if not isinstance(result, dict):
        raise SessionIntegrityError('session record must be an object')
    return result


class SessionStore:
    def __init__(self, path, provider, *, mode='encrypted', keyring=None, dedicated=False):
        requested = Path(path)
        self.path = physical_path(requested.parent) / requested.name
        self.provider = provider
        self.mode = mode
        self.keyring = session_keyring if keyring is None else keyring
        self.dedicated = dedicated

    @contextmanager
    def lock(self):
        if self.dedicated:
            self.path.parent.parent.mkdir(parents=True, exist_ok=True)
            make_private_directory(self.path.parent)
        with profile_lock(self.path.with_suffix('.lock')):
            for target in (self.path, self.path.with_suffix('.session-id.json')):
                for pending in target.parent.glob(f'.{target.name}.*.tmp'):
                    try:
                        info = pending.lstat()
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                        pending.unlink(missing_ok=True)
            yield

    def execution_gate(self):
        return advisory_lock(
            self.path.with_suffix('.session-gate.lock'),
            lambda: ProfileBusy('provider session is busy; retry after its current request'),
            retry_delays=(0.1,) * 10,
            retain_on_cleanup_failure=True,
        )

    def snapshot(self):
        document = _read(self.path)
        if document is None:
            return {}
        if document.get('format') == FORMAT:
            if document.get('provider') != self.provider:
                raise ConfigError('session path already belongs to another provider')
            if not (
                isinstance(document.get('profile_id'), str)
                and type(document.get('generation')) is int
                and type(document.get('revision')) is int
            ):
                raise SessionIntegrityError('invalid session identity')
        elif set(document) - {'cookies', 'origins'}:
            raise SessionIntegrityError('unsupported session format')
        return document

    def _account(self, record):
        return f'{self.provider}/{record["profile_id"]}'

    def credentials(self, record):
        if not record:
            return None
        if record.get('format') != FORMAT:
            return record
        if 'ciphertext' not in record:
            return record.get('state')
        key = self.keyring('get', self._account(record))
        if key is None:
            raise SessionIntegrityError('session encryption key is missing')
        try:
            payload = json.loads(Fernet(key).decrypt(record['ciphertext'].encode('ascii')))
            if payload['account'] != self._account(record):
                raise ValueError('credential identity mismatch')
            return payload['state']
        except (InvalidToken, KeyError, TypeError, ValueError):
            raise SessionIntegrityError('session credential cannot be authenticated') from None

    def _write(self, record):
        write_document_bytes(self.path, document_bytes(record), private=True)

    def _new_record(self, previous, status, *, pending=None):
        identity_path = self.path.with_suffix('.session-id.json')
        identity = _read(identity_path)
        profile_id = previous.get('profile_id')
        if identity is not None:
            if identity.get('provider') != self.provider:
                raise ConfigError('session path already belongs to another provider')
            profile_id = profile_id or identity['profile_id']
        if profile_id is None:
            profile_id = uuid4().hex
            write_document_bytes(
                identity_path,
                document_bytes(
                    {
                        'provider': self.provider,
                        'profile_id': profile_id,
                    }
                ),
                private=True,
            )
        return {
            'format': FORMAT,
            'provider': self.provider,
            'profile_id': profile_id,
            'generation': previous.get('generation', 0) + 1,
            'revision': 0,
            'status': status,
            'pending': pending,
        }

    def _protect(self, record, state, *, encrypted):
        if encrypted:
            account = self._account(record)
            key = self.keyring('set', account, Fernet.generate_key().decode('ascii'))
            record['ciphertext'] = (
                Fernet(key)
                .encrypt(document_bytes({'account': account, 'state': state}))
                .decode('ascii')
            )
            record.pop('state', None)
        else:
            record['state'] = state

    def _remove_old_metadata(self):
        self.path.with_suffix('.status.json').unlink(missing_ok=True)

    def install(self, previous, state, status, *, metadata=None, pending=None):
        with self.lock():
            if self.snapshot() != previous:
                return None
            if previous.get('delete_key'):
                raise ConfigError('session key removal is pending; retry applicant setup clear')
            record = self._new_record(previous, status, pending=pending)
            record.update(metadata or {})
            self._protect(
                record, state, encrypted=self.mode == 'encrypted' or 'ciphertext' in previous
            )
            self._write(record)
            self._remove_old_metadata()
            return record

    def active_snapshot(self):
        """Return one consistent revision and its credentials after any migration."""
        with self.lock():
            previous = self.snapshot()
            state = self.credentials(previous)
            if state is None or self.mode != 'encrypted' or 'ciphertext' in previous:
                return previous, state
            if previous.get('format') != FORMAT:
                record = self._new_record(previous, 'unverified')
            else:
                record = {**previous, 'revision': previous['revision'] + 1}
            self._protect(record, state, encrypted=True)
            self._write(record)
            self._remove_old_metadata()
            return record, state

    def active(self):
        return self.active_snapshot()[1]

    def reconcile(self, action):
        """Apply only the current pending action and acknowledge it atomically."""
        with self.lock():
            record = self.snapshot()
            pending = record.get('pending')
            if not pending:
                return record, {'status': 'unchanged'}
            result = action(pending)
            if result['status'] not in (
                'database_busy',
                'database_error',
                'schema_incompatible',
                'leased',
            ):
                record = {**record, 'pending': None, 'revision': record['revision'] + 1}
                self._write(record)
            return record, result

    def update(self, previous, *, state=_UNSET, **fields):
        with self.lock():
            current = self.snapshot()
            if current != previous or current.get('format') != FORMAT:
                return None
            record = {**current, **fields, 'revision': current['revision'] + 1}
            if state is not _UNSET:
                self._protect(
                    record, state, encrypted='ciphertext' in current or self.mode == 'encrypted'
                )
            self._write(record)
            return record

    def clear(self):
        with self.lock():
            previous = self.snapshot()
            record = (
                previous
                if previous.get('status') == 'cleared'
                else self._new_record(previous, 'cleared', pending='clear')
            )
            if 'ciphertext' in previous or (
                not previous and self.path.with_suffix('.session-id.json').is_file()
            ):
                record['delete_key'] = self.mode == 'encrypted' or 'ciphertext' in previous
            self._write(record)
            self._remove_old_metadata()
            if record.pop('delete_key', False):
                self.keyring('delete', self._account(record))
                self._write(record)
            return record


def read_session_file(path, *, expected_provider=None):
    """Read a legacy file or a managed record from a direct browser adapter."""
    document = _read(Path(path))
    if document and document.get('format') == FORMAT:
        if expected_provider and document['provider'] != expected_provider:
            raise ConfigError('session path already belongs to another provider')
        return SessionStore(path, document['provider']).credentials(document)
    return document
