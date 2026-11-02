"""Strict ranking selection from a file, falling back to stored collection rows."""

from __future__ import annotations

import json
import math
import os
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


def listing_snapshot(
    path: str | os.PathLike[str], *, store: Store | None = None
) -> tuple[Job, ...]:
    """Read once, validate every record, and preserve order without any writes.

    A present file wins even when empty or malformed. Only absence permits the
    database fallback. Unknown legacy job fields are accepted as Job.from_dict
    does; wrapper errors, invalid jobs and duplicate board identities fail closed.
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
    jobs = []
    seen = set()
    for index, item in enumerate(items):
        try:
            if not isinstance(item, dict):
                raise ValueError('job must be an object')
            job = Job.from_dict(item)
            identity = key(job.model_dump())
            if identity in seen:
                raise ValueError('duplicate job identity')
            seen.add(identity)
            jobs.append(job)
        except ValueError as error:
            raise ConfigError(f'invalid listing {path}, list[{index}]: {error}') from error
    return tuple(jobs)
