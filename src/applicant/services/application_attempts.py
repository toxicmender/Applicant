"""Private durable journal for LinkedIn submission and reconciliation."""

import hashlib
import hmac
import json
import logging
import re
import secrets
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..domain.canonical import content_hash
from ..domain.contracts import now
from ..errors import ApplicationBlocked, ConfigError, ExportConflict, SourceError
from ..files import document_bytes, write_document_bytes
from ..infra.path_identity import lock_identity, physical_path
from ..infra.profile_lock import profile_lock
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.financials_lock import export_lock
from ..infra.store.publication import durable_replace
from ..infra.store.repositories import Backend
from .application_projection import STATUSES, projection_bytes, publish_file
from .attempt_import import merge, snapshots

logger = logging.getLogger(__name__)
HISTORY = 'https://www.linkedin.com/help/linkedin/answer/a512329/'


class AttemptJournal:
    def __init__(self, log, session, retry=None, backend: Backend = 'files'):
        self.log = physical_path(log)
        profile = physical_path(session)
        self.profile = profile
        self.path = profile.with_name(f'.{profile.name}.application-attempts-v2.json')
        self.key_path = profile.with_name(f'.{profile.name}.application-attestation.key')
        self.account = lock_identity(profile)
        self.retry = retry
        self.backend: Backend = backend

    def exists(self) -> bool:
        if self.path.is_file() or self._legacy_path.is_file():
            return True
        if self.backend == 'sqlite':
            from ..infra.store.sqlite import DB_NAME

            return (self.log.parent / DB_NAME).is_file()
        return False

    @property
    def _legacy_path(self):
        return self.log.with_name(f'.{self.log.name}.attempts.json')

    @staticmethod
    def index_path(log):
        return log.with_name(f'.{log.name}.attempt-index.json')

    def _legacy(self):
        from ..infra.store.sqlite import Store

        entries = read_ranking_document(self._legacy_path) if self._legacy_path.is_file() else {}
        if self.backend == 'sqlite':
            from ..infra.store.sqlite import DB_NAME

            database = self.log.parent / DB_NAME
            if database.is_file():
                with Store.open_readonly(database) as store:
                    name = store._managed_name('applications', self.log)
                    row = store.db.execute(
                        'SELECT payload FROM application_attempt_journals WHERE log=?', (name,)
                    ).fetchone()
                    if row is not None:
                        payload = json.loads(row[0])
                        for identifier, entry in payload.items():
                            if identifier in entries and entries[identifier] != entry:
                                raise ExportConflict(
                                    f'{self.log}: conflicting legacy attempt {identifier}'
                                )
                            entries[identifier] = entry
        return {
            identifier: {**entry, 'account': self.account, 'destination': str(self.log)}
            for identifier, entry in entries.items()
            if lock_identity(physical_path(entry['account'])) == self.account
        }

    @contextmanager
    def _locked(self, *, full=False):
        with export_lock(self.path):
            document = (
                read_ranking_document(self.path)
                if self.path.exists()
                else {'schema_version': 2, 'entries': {}, 'legacy_destinations': []}
            )
            if document.get('schema_version') != 2:
                raise ConfigError(f'{self.path}: unsupported application journal schema')
            entries = document['entries']
            destination = lock_identity(self.log)
            if destination not in document['legacy_destinations']:
                for identifier, entry in self._legacy().items():
                    entry.setdefault('backend', self.backend)
                    entries.setdefault(identifier, entry)
                document['legacy_destinations'].append(destination)

            def save(data):
                document['entries'] = data
                write_document_bytes(self.path, document_bytes(document), replace=durable_replace)

            yield (document if full else entries), save

    def import_legacy(self, paths):
        candidates = [
            (
                path,
                {
                    identifier: {**entry, 'account': self.account, 'destination': str(self.log)}
                    for identifier, entry in entries.items()
                    if lock_identity(physical_path(entry['account'])) == self.account
                },
            )
            for path, entries in snapshots(paths)
        ]
        with self.profile_owner(), self._locked(full=True) as (document, save):
            merge(document, candidates)
            save(document['entries'])

    def conflicts(self):
        with self._locked(full=True) as (document, _):
            return {
                identifier: {
                    'journal': _brief(document['entries'].get(identifier)),
                    'incoming': {
                        digest: {**_brief(candidate['entry']), 'sources': candidate['sources']}
                        for digest, candidate in incoming.items()
                    },
                }
                for identifier, incoming in document.get('conflicts', {}).items()
            }

    def resolve_conflict(self, identifier, choice, digest):
        with self.profile_owner(), self._locked(full=True) as (document, save):
            entries = document['entries']
            conflicts = document.get('conflicts', {})
            incoming = conflicts.get(identifier)
            if not incoming:
                raise ConfigError(f'no quarantined attempt {identifier}')
            if choice in ('journal', 'both') and identifier not in entries:
                raise ConfigError('no canonical journal record; select an incoming candidate')
            if digest is None:
                if len(incoming) != 1:
                    raise ConfigError('more than one incoming candidate; supply --digest')
                digest = next(iter(incoming))
            if digest not in incoming:
                raise ConfigError('unknown candidate digest')
            if choice in ('incoming', 'both'):
                candidate = dict(incoming[digest]['entry'])
                if not candidate.get('job_key'):
                    raise ConfigError(
                        'cannot activate a candidate with an unknown posting; dismiss it'
                    )
                candidate.pop('attestation_tag', None)
                candidate.pop('attestation_key_id', None)
                key = identifier if choice == 'incoming' else f'{identifier}:legacy-{digest}'
                candidate['synthetic'] = choice == 'both'
                if candidate['state'] in STATUSES:
                    self._enqueue(document, key, candidate)
                entries[key] = candidate
            # Preserve each candidate and its source paths in the private audit.
            resolved = incoming if choice == 'incoming' else {digest: incoming[digest]}
            for key, value in resolved.items():
                document.setdefault('resolved_conflicts', []).append(
                    {
                        'attempt': identifier,
                        'digest': key,
                        'decision': choice,
                        **value,
                    }
                )
            for key in list(resolved):
                incoming.pop(key)
            if not incoming:
                conflicts.pop(identifier)
            save(entries)

    def profile_owner(self):
        return profile_lock(self.profile.with_suffix('.lock'))

    def _key(self, *, create=False):
        try:
            key = self.key_path.read_bytes()
        except FileNotFoundError as error:
            if not create:
                raise ConfigError('retry attestation key is missing; attest again') from error
            key = secrets.token_bytes(32)
            write_document_bytes(self.key_path, key, replace=durable_replace)
        except OSError as error:
            raise ConfigError('retry attestation key is unreadable') from error
        if len(key) != 32:
            raise ConfigError('retry attestation key is invalid; rotate it and attest again')
        return key

    def _session_tag(self, key):
        try:
            state = json.loads(self.profile.read_bytes())
        except (OSError, ValueError) as error:
            raise ConfigError(
                'saved LinkedIn session is unavailable; sign in and attest again'
            ) from error
        cookies = state.get('cookies') if isinstance(state, dict) else state
        if not isinstance(cookies, list):
            raise ConfigError('saved LinkedIn session has no usable cookies; attest again')
        values = {
            cookie['value']
            for cookie in cookies or []
            if isinstance(cookie, dict)
            and cookie.get('name') == 'li_at'
            and isinstance(cookie.get('value'), str)
            and cookie['value']
            and str(cookie.get('domain', '')).lstrip('.') in ('linkedin.com', 'www.linkedin.com')
        }
        if len(values) != 1:
            raise ConfigError(
                'saved LinkedIn session is unavailable or ambiguous; sign in and attest again'
            )
        value = values.pop().encode('utf-8')
        return hmac.digest(key, value, 'sha256').hex()

    def rotate(self):
        with self.profile_owner(), self._locked():
            write_document_bytes(self.key_path, secrets.token_bytes(32), replace=durable_replace)

    def _check_attestation(self, entry):
        key_id, tag = entry.get('attestation_key_id'), entry.get('attestation_tag')
        if not isinstance(key_id, str) or not isinstance(tag, str):
            raise ConfigError('retry authorization requires fresh portal attestation')
        key = self._key()
        if key_id != hashlib.sha256(key).hexdigest()[:16]:
            raise ConfigError('retry attestation key rotated; attest again')
        if not hmac.compare_digest(tag, self._session_tag(key)):
            raise ConfigError('saved LinkedIn session changed; attest again')

    def detail(self, identifier: str) -> dict[str, Any]:
        if not self.exists():
            raise ConfigError('attempt not found for this application log and account')
        with self._locked() as (entries, _):
            entry = entries.get(identifier)
            if entry is None or entry['account'] != self.account:
                raise ConfigError('attempt not found for this application log and account')
            return entry

    def _register(self, document, target, backend):
        index = self.index_path(target)
        with export_lock(target):
            registry = (
                read_ranking_document(index)
                if index.is_file()
                else {'schema_version': 1, 'journals': {}, 'tombstones': {}}
            )
            if registry.get('schema_version') != 1:
                raise ConfigError(f'{index}: unsupported projection index schema')
            if 'journal_id' not in document:
                document['journal_id'] = uuid4().hex
            journal_id = document['journal_id']
            if registry['journals'].get(journal_id, {}).get('profile') != str(self.profile):
                registry['journals'][journal_id] = {
                    'profile': str(self.profile),
                    'registration_id': uuid4().hex,
                    'backend': backend,
                }
                registry['tombstones'].pop(journal_id, None)
                write_document_bytes(index, document_bytes(registry), replace=durable_replace)
            destination = lock_identity(target)
            registered = document.setdefault('registered_destinations', [])
            if destination not in registered:
                registered.append(destination)

    def _enqueue(self, document, identifier, entry):
        target = physical_path(entry['destination'])
        if lock_identity(target) not in document.get('registered_destinations', []):
            self._register(document, target, entry.get('backend', self.backend))
        document.setdefault('projection', []).append(
            {
                'event_id': uuid4().hex,
                'attempt': identifier,
                'entry': dict(entry),
            }
        )

    def recover_confirmed(self) -> None:
        """Drain the projection events registered for this destination."""
        with (
            self.profile_owner(),
            self._locked(full=True) as (document, save),
            export_lock(self.log),
        ):
            self._drain(document, lambda: save(document['entries']))

    def _drain(self, document, checkpoint):
        from ..infra.store.sqlite import Store

        queue = document.get('projection', [])
        for event in list(queue):
            entry = event['entry']
            if lock_identity(physical_path(entry['destination'])) != lock_identity(self.log):
                continue
            try:
                if entry.get('backend', self.backend) == 'sqlite':
                    with Store.beside(self.log) as store:
                        store.project_application(
                            self.log,
                            event['event_id'],
                            lambda raw, attempt=event['attempt'], row=entry: projection_bytes(
                                raw, attempt, row
                            ),
                        )
                else:
                    publish_file(self.log, event, checkpoint)
            except ValueError as error:
                raise ExportConflict(f'{self.log}: {error}') from error
            queue.remove(event)
            checkpoint()

    def begin(self, job, *, armed=False):
        url = urlsplit(job['url'])
        match = re.fullmatch(r'/jobs/view/(?:[^/]+-)?(\d+)/?', url.path)
        target = match[1] if match else job.get('id') or url.path.rstrip('/')
        key = content_hash(['linkedin', str(target)])
        with self._locked(full=True) as (document, save):
            entries = document['entries']
            for identifier, group in document.get('conflicts', {}).items():
                affected = {candidate['entry'].get('job_key') for candidate in group.values()}
                if identifier in entries:
                    affected.add(entries[identifier].get('job_key'))
                if key in affected or None in affected:
                    raise ApplicationBlocked(
                        f'quarantined legacy attempt {identifier}; run apply --list-conflicts, '
                        f'then apply --resolve-conflict {identifier} --keep incoming --digest HASH'
                    )
            relevant = {i: entry for i, entry in entries.items() if entry['job_key'] == key}
            unresolved = next(
                (
                    i
                    for i, entry in relevant.items()
                    if entry['state'] in ('armed', 'uncertain', 'submitted', 'abandoned')
                ),
                None,
            )
            if unresolved:
                raise ApplicationBlocked(
                    f'LinkedIn application already attempted: {unresolved}; check {HISTORY}'
                )
            previous = relevant.get(self.retry)
            eligible = [
                identifier
                for identifier, entry in relevant.items()
                if entry['state'] == 'not-submitted'
            ]
            if eligible and (
                previous is None
                or previous['state'] != 'not-submitted'
                or previous['account'] != self.account
            ):
                raise SourceError(
                    f'LinkedIn retry requires explicit acknowledgement for {eligible[-1]}'
                )
            if self.retry is not None and (
                previous is None or previous['state'] != 'not-submitted'
            ):
                raise SourceError('LinkedIn retry authorization does not match this posting')
            identifier = uuid4().hex
            if self.retry is not None and previous is not None:
                self._check_attestation(previous)
                previous['state'] = 'retried'
                previous['retry_attempt'] = identifier
            self._register(document, self.log, self.backend)
            entries[identifier] = {
                'job_key': key,
                'job': job,
                'account': self.account,
                'destination': str(self.log),
                'backend': self.backend,
                'state': 'armed' if armed else 'uncertain',
                'retry_of': self.retry,
                'recorded_at': now().isoformat(),
            }
            save(entries)
            logger.info(
                'LinkedIn application attempt %s; check %s if uncertain', identifier, HISTORY
            )
            return identifier

    def resolve(self, identifier: str, state: str, *, attested: bool = False) -> None:
        with self._locked(full=True) as (document, save):
            entries = document['entries']
            entry = entries.get(identifier)
            if entry is None or entry['account'] != self.account:
                raise ConfigError('attempt not found for this account')
            previous_state = entry['state']
            allowed = {
                'armed': (
                    'submitted',
                    'uncertain',
                    'failed-before-dispatch',
                    'abandoned',
                    'not-submitted',
                ),
                'uncertain': ('submitted', 'not-submitted', 'abandoned'),
                'abandoned': ('not-submitted',),
                'not-submitted': ('not-submitted',),
            }
            if state not in allowed.get(previous_state, ()) or (
                state in ('not-submitted', 'abandoned') and not attested
            ):
                raise ConfigError(
                    'confirm the original account and portal history before unlocking retry'
                )
            entry.update(state=state, resolved_at=now().isoformat(), portal_attested=attested)
            if state == 'not-submitted':
                key = self._key(create=True)
                entry['attestation_key_id'] = hashlib.sha256(key).hexdigest()[:16]
                entry['attestation_tag'] = self._session_tag(key)
                entry['warning'] = 'Retrying may create a duplicate application.'
                logger.warning('LinkedIn attempt %s: %s', identifier, entry['warning'])
            elif state == 'failed-before-dispatch':
                previous = entries.get(entry.get('retry_of'))
                if previous is not None and previous.get('retry_attempt') == identifier:
                    previous['state'] = 'not-submitted'
                    previous.pop('retry_attempt', None)
            if state in STATUSES and entry.get('job', {}).get('title'):
                self._enqueue(document, identifier, entry)
            save(entries)

    def recover_armed(self):
        with self.profile_owner(), self._locked() as (entries, save):
            armed = [entry for entry in entries.values() if entry['state'] == 'armed']
            for entry in armed:
                entry['state'] = 'uncertain'
            if armed:
                save(entries)

    @classmethod
    def drain_registered(cls, log, backend):
        """Try the destination index and each owner without waiting on advisory locks."""
        from ..errors import ApplicantError
        from ..infra.store.sqlite import DB_NAME

        target = physical_path(log)
        index = cls.index_path(target)
        if not (index.exists() or target.exists() or (target.parent / DB_NAME).exists()):
            return False
        try:
            with export_lock(target):
                if not index.exists():
                    return False
                snapshot = read_ranking_document(index)
                if snapshot.get('schema_version') != 1:
                    return True
        except ApplicantError:
            return True
        deferred = False
        for journal_id, registration in snapshot['journals'].items():
            journal = cls(
                target, registration['profile'], backend=registration.get('backend', backend)
            )
            if not journal.path.parent.is_dir():
                deferred = True
                continue
            try:
                with (
                    journal.profile_owner(),
                    journal._locked(full=True) as (document, save),
                    export_lock(target),
                ):
                    current = read_ranking_document(index)
                    if current['journals'].get(journal_id) != registration:
                        deferred = True
                        continue
                    if not journal.path.is_file():
                        current['tombstones'][journal_id] = current['journals'].pop(journal_id)
                        write_document_bytes(
                            index, document_bytes(current), replace=durable_replace
                        )
                    elif document.get('journal_id') == journal_id:
                        journal._drain(document, lambda: save(document['entries']))
                    else:
                        deferred = True
            except ApplicantError:
                deferred = True
        return deferred


def _brief(entry):
    if entry is None:
        return {}
    return {
        'posting': (entry.get('job') or {}).get('id'),
        'state': entry.get('state'),
        'timestamp': entry.get('recorded_at'),
        'destination': entry.get('destination'),
    }
