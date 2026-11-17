"""Query-scoped enrichment requests stored with their posting."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import timedelta
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain import flags
from ..domain.canonical import content_hash
from ..domain.dedupe import key
from ..domain.filtering import JobFilter
from ..domain.job import Job, experience_from
from ..errors import NotFound
from ..files import read_document, write_document
from ..financials.tracker import _utc_timestamp
from ..infra.interrupts import defer_interrupt
from ..infra.store.financials_lock import export_lock
from ..infra.store.repositories import Backend
from ..infra.store.sqlite import DB_NAME, Store
from .description_cache import DescriptionCache, visible_card

if TYPE_CHECKING:
    from .search import Board


def _signature(source, keywords, filters):
    parameters = {name: value for name, value in vars(filters).items() if name != 'rates'}
    parameters['keywords'] = ' '.join(keywords.casefold().split())
    return content_hash([source, 'experience-v1', parameters])


def _fresh(value, current, days):
    stamp = _utc_timestamp(value)
    return stamp is not None and -timedelta(minutes=2) <= current - stamp < timedelta(days=days)


def _metadata(item):
    return item.get('enrichment_v1') or {}


def _envelope(job, metadata, *, vetted):
    serialized = job.to_dict()
    if vetted:
        return {**serialized, 'enrichment_v1': metadata}
    return {
        'source': job.source,
        'id': job.id,
        'url': job.url,
        'pending_job_v1': serialized,
        'enrichment_v1': metadata,
    }


def _experience(job, description):
    match = experience_from(description)
    if match is not None:
        job.experience_text, job.experience_min, job.experience_max = match
        job.flags = list(dict.fromkeys([*job.flags, flags.EXPERIENCE_ENRICHED]))
    return job


class PendingEnrichment:
    def __init__(self, output, backend: Backend, *, clock=None):
        self.path = Path(output)
        self.backend: Backend = backend
        self.cache = DescriptionCache(self.path, clock=clock)

    def _mutate(self, update):
        with defer_interrupt():
            if self.backend == 'sqlite':
                with Store.managed(self.path, 'jobs') as (store, target):
                    return store.update_listing_payloads(target, update)
            with export_lock(self.path):
                document = read_document(self.path)
                items = {key(item): item for item in document.get('list', [])}
                changed = update(items)
                if changed:
                    write_document(self.path, {**document, 'list': list(items.values())})
                return changed

    def _snapshot(self):
        if self.backend == 'sqlite' and (self.path.parent / DB_NAME).is_file():
            with Store.managed(self.path, 'jobs') as (store, target):
                return store._job_items(target)
        return read_document(self.path).get('list', [])

    def queue(self, jobs, keywords, filters):
        """Durably register candidates before any description request starts."""
        jobs = list(jobs)
        now = self.cache.clock.current()
        stamp = now.isoformat()
        criterion = {
            'experience': filters.experience,
            'keep_unknown': filters.keep_unknown,
            'keep_unpublished': filters.keep_unpublished,
        }
        candidates = iter(jobs)
        while batch := list(islice(candidates, 25)):

            def update(items, batch=batch):
                for job in batch:
                    identifier = key(job.to_dict())
                    current = items.get(identifier) or {}
                    meta = _metadata(current)
                    pending = {
                        name: request
                        for name, request in meta.get('pending_enrichments', {}).items()
                        if _fresh(request['seen'], now, 14)
                        or ((_utc_timestamp(request['seen']) or now) > now)
                    }
                    matched = {
                        name: request
                        for name, request in meta.get('matched_queries', {}).items()
                        if _fresh(request['at'], now, 30)
                        and all(
                            request.get('card', {}).get(field) in (None, value)
                            for field, value in visible_card(job).items()
                        )
                    }
                    signature = _signature(job.source, keywords, filters)
                    if signature not in matched:
                        created = pending.get(signature, {}).get('created', stamp)
                        pending[signature] = {
                            'criterion': criterion,
                            'created': created,
                            'seen': stamp,
                            'card': job.to_dict(),
                        }
                    pending = dict(
                        sorted(pending.items(), key=lambda pair: (pair[1]['seen'], pair[0]))[-10:]
                    )
                    updated = {'pending_enrichments': pending, 'matched_queries': matched}
                    if current and 'pending_job_v1' not in current:
                        items[identifier] = {**current, 'enrichment_v1': updated}
                    else:
                        items[identifier] = _envelope(job, updated, vetted=False)
                return len(batch)

            self._mutate(update)

    def finish(self, job, requests, *, description=None, terminal=False):
        now = self.cache.clock.current()
        identifier = key(job.to_dict())

        accepted = 0

        def update(items):
            nonlocal accepted
            item = items.get(identifier)
            if item is None:
                return 0
            if terminal:
                pending = _metadata(item).get('pending_enrichments', {})
                if any(pending.get(name) != request for name, request in requests.items()):
                    return 0
                items.pop(identifier)
                return len(pending)
            meta = _metadata(item)
            pending = dict(meta.get('pending_enrichments', {}))
            matched = dict(meta.get('matched_queries', {}))
            promoted = None
            resolved = 0
            for signature, request in requests.items():
                if pending.get(signature) != request:
                    continue
                candidate = _experience(Job.from_dict(request['card']), description)
                keep, found = JobFilter(**request['criterion']).matches(candidate)
                pending.pop(signature)
                resolved += 1
                if keep:
                    accepted += 1
                    candidate.flags = list(dict.fromkeys([*candidate.flags, *found]))
                    promoted = candidate
                    matched[signature] = {
                        'criterion': request['criterion'],
                        'at': now.isoformat(),
                        'card': visible_card(Job.from_dict(request['card'])),
                    }
            if resolved:
                matched = dict(
                    sorted(matched.items(), key=lambda pair: (pair[1]['at'], pair[0]))[-25:]
                )
                updated = {'pending_enrichments': pending, 'matched_queries': matched}
                if promoted is not None:
                    items[identifier] = _envelope(promoted, updated, vetted=True)
                elif 'pending_job_v1' not in item or pending:
                    items[identifier] = {**item, 'enrichment_v1': updated}
                else:
                    items.pop(identifier)
            return resolved

        total = self._mutate(update)
        return accepted, total - accepted

    def _pending(self, sources, signature=None):
        entries = []
        now = self.cache.clock.current()
        for item in self._snapshot():
            if item['source'] not in sources:
                continue
            requests = {
                name: request
                for name, request in _metadata(item).get('pending_enrichments', {}).items()
                if (signature is None or signature == name) and _fresh(request['seen'], now, 14)
            }
            if requests:
                entries.append((item, requests))
        return sorted(
            entries,
            key=lambda pair: (min(r['created'] for r in pair[1].values()), str(key(pair[0]))),
        )

    def resume(
        self,
        sources: Sequence[str],
        client_for: Callable[[str], Board],
        budget: int,
        *,
        signature: str | None = None,
    ) -> dict[str, int]:
        entries = self._pending(sources, signature)
        missing = []
        promoted, pruned = 0, self._expire(sources)
        for item, requests in entries:
            job = Job.from_dict(item.get('pending_job_v1', item))
            found, description = self.cache.read(job)
            if found:
                accepted, rejected = self.finish(job, requests, description=description)
                promoted += accepted
                pruned += rejected
            else:
                missing.append((job, requests))
        clients = {}
        used = 0
        try:
            for job, requests in missing:
                if used >= budget:
                    break
                if job.source not in clients:
                    clients[job.source] = client_for(job.source)
                method = getattr(clients[job.source], 'describe', None)
                if not callable(method):
                    continue
                used += 1
                try:
                    description = method(job)
                except NotFound:
                    _, rejected = self.finish(job, requests, terminal=True)
                    pruned += rejected
                    continue
                self.cache.write(job, description)
                accepted, rejected = self.finish(job, requests, description=description)
                promoted += accepted
                pruned += rejected
        finally:
            from .search import close_quietly

            for name, client in clients.items():
                close_quietly(name, client)
        return {
            'promoted': promoted,
            'pruned': pruned,
            'remaining': sum(len(r) for _, r in self._pending(sources)),
            'live_requests': used,
        }

    def _expire(self, sources):
        now = self.cache.clock.current()
        stale = []
        for item in self._snapshot():
            if item['source'] not in sources:
                continue
            pending = _metadata(item).get('pending_enrichments', {})
            expired = {
                name
                for name, request in pending.items()
                if (stamp := _utc_timestamp(request['seen'])) is None
                or now - stamp >= timedelta(days=14)
            }
            if expired:
                stale.append((item, expired))
        total = 0
        candidates = iter(stale)
        while batch := list(islice(candidates, 25)):

            def update(items, batch=batch):
                changed = 0
                for item, expired in batch:
                    identifier = key(item)
                    if items.get(identifier) != item:
                        continue
                    meta = _metadata(item)
                    pending = {
                        name: value
                        for name, value in meta['pending_enrichments'].items()
                        if name not in expired
                    }
                    if not pending and 'pending_job_v1' in item:
                        items.pop(identifier)
                    else:
                        items[identifier] = {
                            **item,
                            'enrichment_v1': {**meta, 'pending_enrichments': pending},
                        }
                    changed += len(expired)
                return changed

            total += self._mutate(update)
        return total

    def resolve_current(self, job, keywords, filters, description):
        signature = _signature(job.source, keywords, filters)
        identifier = key(job.to_dict())
        for row, requests in self._pending([job.source], signature):
            if key(row) == identifier:
                return self.finish(job, requests, description=description)
        return (0, 0)
