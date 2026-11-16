"""Private durable journal for LinkedIn submission and reconciliation."""

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from ..domain.canonical import content_hash
from ..domain.contracts import now
from ..errors import ConfigError, SourceError
from ..files import document_bytes, write_document_bytes
from ..infra.ranking_documents import read_ranking_document
from ..infra.store.financials_lock import export_lock
from ..infra.store.publication import durable_replace
from ..infra.store.repositories import Backend

logger = logging.getLogger(__name__)
HISTORY = 'https://www.linkedin.com/help/linkedin/answer/a512329/'


class AttemptJournal:
    def __init__(self, log, session, retry=None, backend: Backend = 'files'):
        self.log = Path(log).resolve()
        self.path = self.log.with_name(f'.{self.log.name}.attempts.json')
        self.account = str(Path(session).resolve())
        self.retry = retry
        self.backend: Backend = backend

    def exists(self) -> bool:
        if self.backend == 'sqlite':
            from ..infra.store.sqlite import DB_NAME

            return (self.log.parent / DB_NAME).is_file()
        return self.path.is_file()

    @contextmanager
    def _locked(self):
        with export_lock(self.path):
            if self.backend == 'sqlite':
                import json

                from ..infra.store.sqlite import Store

                with Store.beside(self.log) as store, store._transaction() as db:
                    name = store._managed_name('applications', self.log)
                    row = db.execute(
                        'SELECT payload FROM application_attempt_journals WHERE log=?', (name,)
                    ).fetchone()

                    def save(data):
                        db.execute(
                            'INSERT OR REPLACE INTO application_attempt_journals VALUES (?, ?)',
                            (name, json.dumps(data, ensure_ascii=False)),
                        )

                    yield (json.loads(row[0]) if row else {}), save
            else:

                def save(data):
                    write_document_bytes(self.path, document_bytes(data), replace=durable_replace)

                yield (read_ranking_document(self.path) if self.path.exists() else {}), save

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
            ]
        if recovered:
            applications(self.log, self.backend).record(recovered)

    def begin(self, job):
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
                    if entry['state'] in ('uncertain', 'submitted')
                ),
                None,
            )
            if unresolved:
                raise SourceError(
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
                previous['state'] = 'retried'
                previous['retry_attempt'] = identifier
            entries[identifier] = {
                'job_key': key,
                'job': job,
                'account': self.account,
                'state': 'uncertain',
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
            if entry is None or entry['account'] != self.account or entry['state'] != 'uncertain':
                raise ConfigError('only an uncertain attempt for this account can be resolved')
            if state not in ('submitted', 'not-submitted') or (
                state == 'not-submitted' and not attested
            ):
                raise ConfigError(
                    'confirm the original account and portal history before unlocking retry'
                )
            entry.update(state=state, resolved_at=now().isoformat(), portal_attested=attested)
            if state == 'not-submitted':
                entry['warning'] = 'Retrying may create a duplicate application.'
                logger.warning('LinkedIn attempt %s: %s', identifier, entry['warning'])
            save(entries)
