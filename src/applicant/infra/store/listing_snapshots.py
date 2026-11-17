"""Strict ranking selection from a file, falling back to stored collection rows."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from ...domain.dedupe import key
from ...domain.job import Job
from ...errors import ConfigError, StoreError
from .sqlite import Store


def _object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f'duplicate JSON key: {name}')
        result[name] = value
    return result


def _constant(value: str) -> None:
    raise ValueError(f'nonfinite JSON number: {value}')


def _float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f'nonfinite JSON number: {value}')
    return number


@dataclass(frozen=True)
class ListingSelection:
    jobs: tuple[Job, ...]
    pending_only: int
    overlaps: int
    resumable: bool


def listing_snapshot(
    path: str | os.PathLike[str], *, store: Store | None = None
) -> tuple[Job, ...]:
    return select_listing(path, store=store).jobs


def select_listing(path: str | os.PathLike[str], *, store: Store | None = None) -> ListingSelection:
    """Read once, validate every record, and preserve order without any writes.

    A present file wins even when empty or malformed. Only absence permits the
    database fallback. Unknown legacy job fields are accepted as Job.from_dict
    does; wrapper errors, invalid jobs and conflicting board identities fail closed.
    """
    try:
        text = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        items = [] if store is None else store.listing_items_snapshot(path)
    except OSError as error:
        raise StoreError(f'could not read listing {path}: {error}') from error
    except UnicodeError as error:
        raise ConfigError(f'invalid listing {path}: {error}') from error
    else:
        try:
            document = json.loads(
                text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float
            )
            if not isinstance(document, dict) or set(document) != {'list'}:
                raise ValueError('expected an object containing only list')
            items = document['list']
            if not isinstance(items, list):
                raise ValueError('list must be an array')
        except ValueError as error:
            raise ConfigError(f'invalid listing {path}: {error}') from error
    jobs: dict[tuple, Job] = {}
    pending: dict[tuple, bool] = {}
    for index, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise ValueError('job must be an object')
            if 'pending_job_v1' in item:
                job, resumable = _pending_job(item)
                identity = key(job.model_dump())
                pending[identity] = pending.get(identity, False) or resumable
                continue
            job = Job.from_dict(item)
            identity = key(job.model_dump())
            prior = jobs.setdefault(identity, job)
            if prior != job:
                raise ValueError('conflicting duplicate job identity')
        except ValueError as error:
            raise ConfigError(f'invalid listing {path}, list[{index}]: {error}') from error
    excluded = pending.keys() - jobs.keys()
    return ListingSelection(
        tuple(jobs.values()),
        len(excluded),
        len(pending) - len(excluded),
        any(pending[identity] for identity in excluded),
    )


def _pending_job(item: dict) -> tuple[Job, bool]:
    required = {'source', 'id', 'url', 'pending_job_v1'}
    if not required <= item.keys() or item.keys() - required - {'enrichment_v1'}:
        raise ValueError('invalid pending job envelope keys')
    job = Job.model_validate(item['pending_job_v1'])
    envelope = Job.model_validate(
        {'title': job.title, **{name: item[name] for name in ('source', 'id', 'url')}}
    )
    if (envelope.source, envelope.id, envelope.url) != (job.source, job.id, job.url):
        raise ValueError('pending job identity does not match its envelope')
    meta = item.get('enrichment_v1')
    if meta is not None and not isinstance(meta, dict):
        raise ValueError('invalid pending enrichment metadata')
    requests = (meta or {}).get('pending_enrichments')
    return job, isinstance(requests, dict) and bool(requests)
