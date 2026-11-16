"""Private durable journal for LinkedIn submission and reconciliation."""

import hashlib
import hmac
import json
import logging
import re
import secrets
from contextlib import contextmanager
from pathlib import Path
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
    def _locked(self):
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
                    entries.setdefault(identifier, entry)
                document['legacy_destinations'].append(destination)

            def save(data):
                document['entries'] = data
                write_document_bytes(self.path, document_bytes(document), replace=durable_replace)

            yield entries, save

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

    def recover_confirmed(self) -> None:
        from ..domain.job import Job
        from ..infra.store.repositories import applications

        with self._locked() as (entries, _):
            recovered = [
                (Job.model_validate(item['job']), 'applied', 'linkedin easy apply')
                for item in entries.values()
                if item['state'] == 'submitted'
                and lock_identity(Path(item['destination'])) == lock_identity(self.log)
            ]
        if recovered:
            applications(self.log, self.backend).record(recovered)

    def begin(self, job, *, armed=False):
        url = urlsplit(job['url'])
        match = re.fullmatch(r'/jobs/view/(?:[^/]+-)?(\d+)/?', url.path)
        target = match[1] if match else job.get('id') or url.path.rstrip('/')
        key = content_hash(['linkedin', str(target)])
        with self._locked() as (entries, save):
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
            entries[identifier] = {
                'job_key': key,
                'job': job,
                'account': self.account,
                'destination': str(self.log),
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
        with self._locked() as (entries, save):
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
            save(entries)

    def recover_armed(self):
        with self.profile_owner(), self._locked() as (entries, save):
            armed = [entry for entry in entries.values() if entry['state'] == 'armed']
            for entry in armed:
                entry['state'] = 'uncertain'
            if armed:
                save(entries)
